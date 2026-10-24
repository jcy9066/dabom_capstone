// Copyright (c) 2026. Licensed under MIT.
#include "patrol_navigation_controller/segmented_rpp_controller.hpp"
#include "patrol_navigation_controller/segment_handoff_guard.hpp"
#include "patrol_navigation_controller/local_motion_sweep.hpp"
#include "patrol_navigation_controller/path_sweep.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <iomanip>
#include <sstream>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <utility>

#include "angles/angles.h"
#include "nav2_core/exceptions.hpp"
#include "nav2_costmap_2d/cost_values.hpp"
#include "nav2_util/node_utils.hpp"
#include "std_msgs/msg/string.hpp"
#include "nav2_util/robot_utils.hpp"
#include "pluginlib/class_list_macros.hpp"
#include "tf2/utils.h"

namespace patrol_navigation_controller
{

SegmentedRPPController::SegmentedRPPController()
: rpp_loader_("nav2_core", "nav2_core::Controller")
{}

void SegmentedRPPController::configure(
  const rclcpp_lifecycle::LifecycleNode::WeakPtr & parent,
  std::string name, std::shared_ptr<tf2_ros::Buffer> tf,
  std::shared_ptr<nav2_costmap_2d::Costmap2DROS> costmap_ros)
{
  auto node = parent.lock();
  if (!node) {
    throw std::runtime_error("SegmentedRPPController requires a live lifecycle node");
  }
  parent_ = parent;
  tf_ = tf;
  costmap_ = std::move(costmap_ros);
  plugin_name_ = std::move(name);
  // Observe exactly the path installed by Nav2's FollowPath controller.
  follow_path_publisher_ = node->create_publisher<std_msgs::msg::String>(
    "/patrol_navigation/applied_follow_path", rclcpp::QoS(10));
  follow_path_instance_ = std::to_string(
    std::chrono::steady_clock::now().time_since_epoch().count());
  follow_path_sequence_ = 0;
  using nav2_util::declare_parameter_if_not_declared;
  declare_parameter_if_not_declared(
    node, plugin_name_ + ".segment_pivot_max_angular", rclcpp::ParameterValue(1.05));
  declare_parameter_if_not_declared(
    node, plugin_name_ + ".segment_pivot_acceleration", rclcpp::ParameterValue(1.0));
  declare_parameter_if_not_declared(
    node, plugin_name_ + ".segment_pivot_yaw_tolerance", rclcpp::ParameterValue(0.12));
  declare_parameter_if_not_declared(
    node, plugin_name_ + ".segment_transfer_position_tolerance",
    rclcpp::ParameterValue(0.08));
  node->get_parameter("controller_frequency", controller_frequency_);
  node->get_parameter(plugin_name_ + ".segment_pivot_max_angular", pivot_max_angular_);
  node->get_parameter(plugin_name_ + ".segment_pivot_acceleration", pivot_acceleration_);
  node->get_parameter(plugin_name_ + ".segment_pivot_yaw_tolerance", pivot_yaw_tolerance_);
  node->get_parameter(
    plugin_name_ + ".segment_transfer_position_tolerance", transfer_position_tolerance_);

  const double positive_limits[] = {
    controller_frequency_, pivot_max_angular_, pivot_acceleration_,
    pivot_yaw_tolerance_, transfer_position_tolerance_,
    transfer_linear_stop_, transfer_angular_stop_
  };
  for (const double limit : positive_limits) {
    if (!std::isfinite(limit) || limit <= 0.0) {
      throw std::runtime_error("Invalid segmented controller limits");
    }
  }

  // Inner RPP shares the FollowPath parameter namespace. The outer
  // RotationShimController delegates path tracking only; its final rotation
  // skips the sweep check when angular_dist_threshold is 3.15 radians.
  // SegmentedRPP owns the swept-collision-checked terminal yaw pivot.
  rpp_ = rpp_loader_.createSharedInstance(
    "nav2_regulated_pure_pursuit_controller::RegulatedPurePursuitController");
  rpp_->configure(parent, plugin_name_, tf, costmap_);
  collision_ = std::make_unique<
    nav2_costmap_2d::FootprintCollisionChecker<nav2_costmap_2d::Costmap2D *>>(
    costmap_->getCostmap());
}

void SegmentedRPPController::activate()
{
  rpp_->activate();
}

void SegmentedRPPController::deactivate()
{
  invalidatePlan();
  rpp_->deactivate();
}

void SegmentedRPPController::cleanup()
{
  if (rpp_) {
    rpp_->cleanup();
    rpp_.reset();
  }
  collision_.reset();
  follow_path_publisher_.reset();
  costmap_.reset();
  invalidatePlan();
}

double SegmentedRPPController::yaw(const geometry_msgs::msg::PoseStamped & pose)
{
  return tf2::getYaw(pose.pose.orientation);
}

double SegmentedRPPController::distance(
  const geometry_msgs::msg::PoseStamped & a,
  const geometry_msgs::msg::PoseStamped & b)
{
  return std::hypot(
    a.pose.position.x - b.pose.position.x,
    a.pose.position.y - b.pose.position.y);
}

geometry_msgs::msg::PoseStamped SegmentedRPPController::inControllerFrame(
  const geometry_msgs::msg::PoseStamped & target,
  const std::string & target_frame) const
{
  geometry_msgs::msg::PoseStamped transformed;
  if (!nav2_util::transformPoseInTargetFrame(
      target, transformed, *tf_, target_frame))
  {
    throw std::runtime_error(
      "Cannot transform lattice segment target to controller frame");
  }
  return transformed;
}

void SegmentedRPPController::buildStages()
{
  stages_.clear();
  stage_index_ = 0;
  const size_t n = path_.poses.size();
  if (n < 2) {
    throw std::runtime_error("Lattice path must contain at least two poses");
  }

  // A nonfinite position or malformed orientation can be incorrectly
  // classified as a forward segment when ordinary comparisons fail.
  for (const auto & stamped : path_.poses) {
    const auto & p = stamped.pose;
    if (!std::isfinite(p.position.x) || !std::isfinite(p.position.y) ||
      !validUnitQuaternion(
        p.orientation.x, p.orientation.y, p.orientation.z, p.orientation.w))
    {
      throw std::runtime_error("Lattice path contains an invalid pose");
    }
  }

  auto kind = [this](size_t index) -> int {
      const auto & p = path_.poses[index];
      const auto & q = path_.poses[index + 1];
      const double separation = distance(p, q);
      const double delta_yaw = angles::shortest_angular_distance(yaw(p), yaw(q));
      if (separation < 0.003 && std::abs(delta_yaw) > 0.03) {
        return delta_yaw > 0 ? 2 : 3;  // spin left, spin right
      }
      if (separation < 0.003) {
        return 4;  // duplicated point with no meaningful motion
      }
      const double dx = q.pose.position.x - p.pose.position.x;
      const double dy = q.pose.position.y - p.pose.position.y;
      const double projection = dx * std::cos(yaw(p)) + dy * std::sin(yaw(p));
      return projection < -0.003 ? 1 : 0;  // reverse, forward
    };

  size_t run_begin = 0;
  int previous_kind = -1;
  for (size_t edge = 0; edge < n - 1; ++edge) {
    const int current_kind = kind(edge);
    if (current_kind == 4) {
      // Treat a duplicate as a path separator, never invent a spin.
      if (previous_kind != -1 && edge > run_begin) {
        stages_.push_back({
          previous_kind >= 2 ? Mode::Pivot : Mode::Drive,
          run_begin, edge, yaw(path_.poses[edge])
        });
      }
      previous_kind = -1;
      run_begin = edge + 1;
      continue;
    }

    if (previous_kind >= 0 && previous_kind != current_kind) {
      stages_.push_back({
        previous_kind >= 2 ? Mode::Pivot : Mode::Drive,
        run_begin, edge, yaw(path_.poses[edge])
      });
      run_begin = edge;
    }
    if (previous_kind < 0) {
      run_begin = edge;
    }
    previous_kind = current_kind;
  }
  if (previous_kind >= 0 && n - 1 > run_begin) {
    stages_.push_back({
      previous_kind >= 2 ? Mode::Pivot : Mode::Drive,
      run_begin, n - 1, yaw(path_.poses.back())
    });
  }
  if (stages_.empty()) {
    throw std::runtime_error("State Lattice path contains no usable motion");
  }
}

void SegmentedRPPController::activateStage()
{
  if (stage_index_ >= stages_.size()) {
    return;
  }
  const auto & stage = stages_[stage_index_];
  if (stage.mode != Mode::Drive) {
    return;
  }
  nav_msgs::msg::Path slice;
  slice.header = path_.header;
  slice.poses.insert(
    slice.poses.end(),
    path_.poses.begin() + stage.begin,
    path_.poses.begin() + stage.end + 1);
  if (slice.poses.size() < 2) {
    throw std::runtime_error("Movement segment is too short for RPP");
  }
  rpp_->setPlan(slice);
}

void SegmentedRPPController::invalidatePlan() noexcept
{
  plan_ready_ = false;
  stages_.clear();
  stage_index_ = 0;
  pivot_started_ = false;
  path_.poses.clear();
}

void SegmentedRPPController::advanceStage()
{
  // Never expose the next stage to computeVelocityCommands if RPP rejects
  // its path slice. A failed intermediate handoff is a latched invalid plan
  // until a new, fully installed path replaces it.
  ++stage_index_;
  pivot_started_ = false;
  try {
    activateStage();
  } catch (...) {
    invalidatePlan();
    throw;
  }
}

void SegmentedRPPController::setPlan(const nav_msgs::msg::Path & path)
{
  // Nav2 can deliver a replacement path while the controller is moving.
  // Invalidate the previous plan BEFORE parsing/replacing stages and before
  // passing a new movement slice to the nested RPP plugin. An exception
  // anywhere in this process must never re-enable its cached old route.
  invalidatePlan();
  try {
    path_ = path;
    buildStages();
    activateStage();
    plan_ready_ = true;
    // Announce the installed path; this does not issue motor commands.
    if (follow_path_publisher_ && path_.poses.size() <= 5000) {
      std::ostringstream json;
      json << std::setprecision(17)
           << "{\"source\":\"segmented_rpp\",\"instance\":\""
           << follow_path_instance_ << "\",\"sequence\":"
           << ++follow_path_sequence_ << ",\"path\":[";
      for (size_t i = 0; i < path_.poses.size(); ++i) {
        const auto & pose = path_.poses[i].pose;
        if (i > 0) {json << ',';}
        json << "{\"x\":" << pose.position.x
             << ",\"y\":" << pose.position.y
             << ",\"yaw\":" << tf2::getYaw(pose.orientation) << '}';
      }
      json << "]}";
      std_msgs::msg::String notice;
      notice.data = json.str();
      follow_path_publisher_->publish(notice);
    }
  } catch (...) {
    invalidatePlan();
    throw;
  }
}

geometry_msgs::msg::TwistStamped SegmentedRPPController::stopped(
  const geometry_msgs::msg::PoseStamped & pose) const
{
  geometry_msgs::msg::TwistStamped command;
  command.header = pose.header;
  return command;
}

geometry_msgs::msg::TwistStamped SegmentedRPPController::checkedCommand(
  const geometry_msgs::msg::PoseStamped & pose,
  const geometry_msgs::msg::TwistStamped & command)
{
  const double linear = command.twist.linear.x;
  const double angular = command.twist.angular.z;
  // Differential-drive projection uses only linear.x and angular.z.
  // Reject other nonzero/non-finite components before the zero-command
  // shortcut, otherwise a malformed "STOP" can bypass the swept check.
  if (!validDifferentialDriveCommand(
      linear, command.twist.linear.y, command.twist.linear.z,
      command.twist.angular.x, command.twist.angular.y, angular))
  {
    throw std::runtime_error("Non-differential or non-finite Nav2 motion command");
  }
  if (std::abs(linear) < 1e-10 && std::abs(angular) < 1e-10) {
    return command;  // A physical STOP never requires costmap clearance.
  }
  if (!costmap_ || !costmap_->isCurrent() || !collision_) {
    throw std::runtime_error("Local costmap stale during Nav2 motion");
  }
  // Controller pose may not use the rolling local costmap's odom frame.
  const auto local_pose = inControllerFrame(pose, costmap_->getGlobalFrameID());
  const auto & position = local_pose.pose.position;
  const auto initial = MotionPose{position.x, position.y, yaw(local_pose)};
  const auto projected = localMotionHorizon(initial, linear, angular);
  if (projected.size() < 2) {
    throw std::runtime_error("Cannot certify Nav2 motion horizon");
  }
  auto * map = costmap_->getCostmap();
  if (!map) {
    throw std::runtime_error("Local costmap missing during Nav2 motion");
  }
  // Use the complete padded chassis at every interpolated position and
  // heading, including pure pivots and reversing. Nav2 Humble IsPathValid
  // checks stored path poses, not the full continuous executed trajectory.
  std::unique_lock<nav2_costmap_2d::Costmap2D::mutex_t> lock(*map->getMutex());
  const auto footprint = costmap_->getRobotFootprint();
  if (footprint.size() < 3 || !costmap_->isCurrent()) {
    throw std::runtime_error("Local chassis or costmap no longer valid");
  }
  const bool safe = validateSweptPath(
    projected, [&](const MotionPose & sample) {
      const double cost = collision_->footprintCostAtPose(
        sample.x, sample.y, sample.yaw, footprint);
      // Includes inscribed 253, lethal 254, unknown 255 and off-map -1.
      return std::isfinite(cost) && cost >= 0.0 &&
        cost < static_cast<double>(
        nav2_costmap_2d::INSCRIBED_INFLATED_OBSTACLE);
    });
  if (!safe) {
    throw std::runtime_error("Local swept chassis obstructed during Nav2 motion");
  }
  return command;
}

void SegmentedRPPController::checkRotationCollision(
  const geometry_msgs::msg::PoseStamped & pose, double angle_error)
{
  if (!std::isfinite(angle_error) ||
    !std::isfinite(pose.pose.position.x) ||
    !std::isfinite(pose.pose.position.y) ||
    !std::isfinite(yaw(pose)))
  {
    throw std::runtime_error("Cannot certify a pivot with non-finite pose");
  }
  if (!costmap_ || !costmap_->isCurrent() || !collision_) {
    throw std::runtime_error("Cannot certify rotation on a stale or missing costmap");
  }
  // The input can be in map or odom; costmap cells are always addressed in
  // the local costmap's global frame. Checking untransformed XY/Yaw against
  // an odom grid would certify the wrong physical patch of ground.
  const auto local_pose = inControllerFrame(pose, costmap_->getGlobalFrameID());
  if (!std::isfinite(local_pose.pose.position.x) ||
    !std::isfinite(local_pose.pose.position.y) ||
    !validUnitQuaternion(
      local_pose.pose.orientation.x, local_pose.pose.orientation.y,
      local_pose.pose.orientation.z, local_pose.pose.orientation.w))
  {
    throw std::runtime_error("Cannot certify rotation with invalid transformed pose");
  }
  auto * costmap = costmap_->getCostmap();
  if (!costmap) {
    throw std::runtime_error("Cannot certify rotation without a costmap");
  }
  std::unique_lock<nav2_costmap_2d::Costmap2D::mutex_t> lock(*(costmap->getMutex()));
  if (!costmap_->isCurrent()) {
    throw std::runtime_error("Local costmap became stale during pivot check");
  }
  const double heading = yaw(local_pose);
  const size_t steps = std::max<size_t>(
    1, static_cast<size_t>(std::ceil(std::abs(angle_error) / 0.05)));
  const auto footprint = costmap_->getRobotFootprint();
  if (footprint.size() < 3) {
    throw std::runtime_error("Cannot certify rotation with invalid robot footprint");
  }
  for (size_t i = 0; i <= steps; ++i) {
    const double test_heading = heading + angle_error * static_cast<double>(i) / steps;
    const double cost = collision_->footprintCostAtPose(
      local_pose.pose.position.x, local_pose.pose.position.y, test_heading, footprint);
    // Identical conservative threshold to the global candidate scorer:
    // 253 is inscribed/inflated, 254 lethal, 255 unknown.
    // No unknown rotation is safe without current positive clearance.
    if (!std::isfinite(cost) || cost < 0.0 ||
      cost >= static_cast<double>(nav2_costmap_2d::INSCRIBED_INFLATED_OBSTACLE))
    {
      throw std::runtime_error("In-place rotation footprint is obstructed");
    }
  }
}

geometry_msgs::msg::TwistStamped SegmentedRPPController::computeVelocityCommands(
  const geometry_msgs::msg::PoseStamped & pose,
  const geometry_msgs::msg::Twist & velocity,
  nav2_core::GoalChecker * goal_checker)
{
  if (!plan_ready_) {
    throw std::runtime_error("Segmented controller has no valid active path");
  }
  if (stage_index_ >= stages_.size()) {
    return stopped(pose);
  }
  // Reject malformed live pose or Odometry before calculating the next
  // command; nonfinite components would bypass transition comparisons.
  if (!std::isfinite(pose.pose.position.x) ||
    !std::isfinite(pose.pose.position.y) ||
    !validUnitQuaternion(
      pose.pose.orientation.x, pose.pose.orientation.y,
      pose.pose.orientation.z, pose.pose.orientation.w) ||
    !std::isfinite(velocity.linear.x) || !std::isfinite(velocity.angular.z))
  {
    throw std::runtime_error("Invalid pose or velocity at segment handoff");
  }
  const auto & stage = stages_[stage_index_];
  if (stage.mode == Mode::Drive) {
    // Never use RotationShim for terminal yaw: its large 3.15rad initial
    // heading threshold causes the Humble collision sweep to skip checks.
    // Once near the final endpoint, pivot using OUR swept-footprint gate.
    if (stage_index_ + 1 >= stages_.size()) {
      const auto terminal = inControllerFrame(
        path_.poses.back(), pose.header.frame_id);
      if (distance(pose, terminal) <= 0.25) {
        const double remaining = angles::shortest_angular_distance(
          yaw(pose), yaw(terminal));
        if (!std::isfinite(remaining)) {
          throw std::runtime_error("Invalid final Goal yaw");
        }
        if (std::abs(remaining) > pivot_yaw_tolerance_) {
          if (!safeToContinuePivot(
              pivot_started_, velocity.linear.x, velocity.angular.z,
              transfer_linear_stop_, transfer_angular_stop_))
          {
            return stopped(pose);
          }
          checkRotationCollision(pose, remaining);
          const double magnitude = std::min(
            pivot_max_angular_,
            std::sqrt(2.0 * pivot_acceleration_ * std::abs(remaining)));
          const double requested = std::copysign(magnitude, remaining);
          const double step = pivot_acceleration_ / controller_frequency_;
          geometry_msgs::msg::TwistStamped command = stopped(pose);
          command.twist.angular.z = std::clamp(
            requested, velocity.angular.z - step, velocity.angular.z + step);
          const auto certified = checkedCommand(pose, command);
          pivot_started_ = true;
          return certified;
        }
        // Final XY and yaw are within the actual goal checker tolerances.
        // Publish a STOP, not a leftover RPP linear command: the server
        // still needs fresh encoder samples before confirming success.
        // In particular, re-entering RPP here can unexpectedly drive
        // forward after a successful final heading correction.
        pivot_started_ = false;
        return stopped(pose);
      }
      return checkedCommand(pose, rpp_->computeVelocityCommands(pose, velocity, goal_checker));
    }
    const auto end_pose = inControllerFrame(
      path_.poses[stage.end], pose.header.frame_id);
    const double to_end = distance(pose, end_pose);
    if (to_end <= transfer_position_tolerance_) {
      if (!stationaryForSegmentTransition(
          velocity.linear.x, velocity.angular.z,
          transfer_linear_stop_, transfer_angular_stop_))
      {
        return stopped(pose);  // Pi STOP + direction interlock owns actual halt
      }
      advanceStage();
      return stopped(pose);
    }
    return checkedCommand(pose, rpp_->computeVelocityCommands(pose, velocity, goal_checker));
  }

  const auto center = inControllerFrame(
    path_.poses[stage.begin], pose.header.frame_id);
  if (distance(pose, center) > transfer_position_tolerance_ + 0.04) {
    throw std::runtime_error("Robot moved away from planned rotation point");
  }
  // Entering a pivot requires full standstill. Once a pivot has started,
  // angular Odometry is expected to be nonzero: do NOT reapply the entry
  // threshold on each tick or the rotation will repeatedly STOP/restart.
  // Translation remains forbidden throughout the pivot. Pi's independent
  // encoder STOP interlock still protects changes of drive direction.
  if (!safeToContinuePivot(
      pivot_started_, velocity.linear.x, velocity.angular.z,
      transfer_linear_stop_, transfer_angular_stop_))
  {
    return stopped(pose);
  }

  const auto rotation_goal = inControllerFrame(
    path_.poses[stage.end], pose.header.frame_id);
  const double remaining = angles::shortest_angular_distance(
    yaw(pose), yaw(rotation_goal));
  if (!std::isfinite(remaining)) {
    throw std::runtime_error("Invalid lattice pivot target orientation");
  }
  if (std::abs(remaining) <= pivot_yaw_tolerance_) {
    if (!stationaryForSegmentTransition(
        velocity.linear.x, velocity.angular.z,
        transfer_linear_stop_, transfer_angular_stop_))
    {
      return stopped(pose);
    }
    advanceStage();
    return stopped(pose);
  }

  // Collision-check the swept chassis footprint, not only the endpoint.
  checkRotationCollision(pose, remaining);
  const double magnitude = std::min(
    pivot_max_angular_, std::sqrt(2.0 * pivot_acceleration_ * std::abs(remaining)));
  const double desired = std::copysign(magnitude, remaining);
  const double maximum_step = pivot_acceleration_ / controller_frequency_;
  geometry_msgs::msg::TwistStamped command = stopped(pose);
  command.twist.angular.z = std::clamp(
    desired, velocity.angular.z - maximum_step, velocity.angular.z + maximum_step);
  const auto certified = checkedCommand(pose, command);
  pivot_started_ = true;
  return certified;
}

void SegmentedRPPController::setSpeedLimit(
  const double & speed_limit, const bool & percentage)
{
  rpp_->setSpeedLimit(speed_limit, percentage);
}

}  // namespace patrol_navigation_controller

PLUGINLIB_EXPORT_CLASS(
  patrol_navigation_controller::SegmentedRPPController, nav2_core::Controller)
