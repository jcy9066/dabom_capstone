// Copyright (c) 2026. Licensed under MIT.
#include "patrol_navigation_controller/segmented_rpp_controller.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <utility>

#include "angles/angles.h"
#include "nav2_core/exceptions.hpp"
#include "nav2_costmap_2d/cost_values.hpp"
#include "nav2_util/node_utils.hpp"
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

  if (controller_frequency_ <= 0.0 || pivot_max_angular_ <= 0.0 ||
    pivot_acceleration_ <= 0.0 || pivot_yaw_tolerance_ <= 0.0 ||
    transfer_position_tolerance_ <= 0.0)
  {
    throw std::runtime_error("Invalid segmented controller limits");
  }

  // Inner RPP shares the FollowPath parameter namespace. The outer
  // RotationShimController retains final goal yaw and its collision check.
  rpp_ = rpp_loader_.createUniqueInstance(
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
  rpp_->deactivate();
}

void SegmentedRPPController::cleanup()
{
  if (rpp_) {
    rpp_->cleanup();
    rpp_.reset();
  }
  collision_.reset();
  costmap_.reset();
  stages_.clear();
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
    throw nav2_core::NoValidControl(
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
    throw nav2_core::NoValidControl("Lattice path must contain at least two poses");
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
    throw nav2_core::NoValidControl("State Lattice path contains no usable motion");
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
    throw nav2_core::NoValidControl("Movement segment is too short for RPP");
  }
  rpp_->setPlan(slice);
}

void SegmentedRPPController::setPlan(const nav_msgs::msg::Path & path)
{
  path_ = path;
  buildStages();
  activateStage();
}

geometry_msgs::msg::TwistStamped SegmentedRPPController::stopped(
  const geometry_msgs::msg::PoseStamped & pose) const
{
  geometry_msgs::msg::TwistStamped command;
  command.header = pose.header;
  return command;
}

void SegmentedRPPController::checkRotationCollision(
  const geometry_msgs::msg::PoseStamped & pose, double angle_error)
{
  auto * costmap = costmap_->getCostmap();
  std::unique_lock<nav2_costmap_2d::Costmap2D::mutex_t> lock(*(costmap->getMutex()));
  const double heading = yaw(pose);
  const size_t steps = std::max<size_t>(
    1, static_cast<size_t>(std::ceil(std::abs(angle_error) / 0.05)));
  const auto footprint = costmap_->getRobotFootprint();
  for (size_t i = 0; i <= steps; ++i) {
    const double test_heading = heading + angle_error * static_cast<double>(i) / steps;
    const double cost = collision_->footprintCostAtPose(
      pose.pose.position.x, pose.pose.position.y, test_heading, footprint);
    if (cost < 0.0 ||
      cost >= static_cast<double>(nav2_costmap_2d::LETHAL_OBSTACLE) ||
      (cost == static_cast<double>(nav2_costmap_2d::NO_INFORMATION) &&
      costmap_->getLayeredCostmap()->isTrackingUnknown()))
    {
      throw nav2_core::NoValidControl("In-place rotation footprint is obstructed");
    }
  }
}

geometry_msgs::msg::TwistStamped SegmentedRPPController::computeVelocityCommands(
  const geometry_msgs::msg::PoseStamped & pose,
  const geometry_msgs::msg::Twist & velocity,
  nav2_core::GoalChecker * goal_checker)
{
  if (stage_index_ >= stages_.size()) {
    return stopped(pose);
  }
  const auto & stage = stages_[stage_index_];
  if (stage.mode == Mode::Drive) {
    // Do not truncate the final segment's normal RPP or terminal goal-yaw
    // behavior: Nav2's outer RotationShim owns the last pose orientation.
    if (stage_index_ + 1 >= stages_.size()) {
      return rpp_->computeVelocityCommands(pose, velocity, goal_checker);
    }
    const auto end_pose = inControllerFrame(
      path_.poses[stage.end], pose.header.frame_id);
    const double to_end = distance(pose, end_pose);
    if (to_end <= transfer_position_tolerance_) {
      if (std::abs(velocity.linear.x) >= transfer_linear_stop_ ||
        std::abs(velocity.angular.z) >= transfer_angular_stop_)
      {
        return stopped(pose);  // Pi STOP + direction interlock owns actual halt
      }
      ++stage_index_;
      activateStage();
      return stopped(pose);
    }
    return rpp_->computeVelocityCommands(pose, velocity, goal_checker);
  }

  const auto center = inControllerFrame(
    path_.poses[stage.begin], pose.header.frame_id);
  if (distance(pose, center) > transfer_position_tolerance_ + 0.04) {
    throw nav2_core::NoValidControl("Robot moved away from planned rotation point");
  }
  if (std::abs(velocity.linear.x) >= transfer_linear_stop_) {
    return stopped(pose);
  }

  const auto rotation_goal = inControllerFrame(
    path_.poses[stage.end], pose.header.frame_id);
  const double remaining = angles::shortest_angular_distance(
    yaw(pose), yaw(rotation_goal));
  if (std::abs(remaining) <= pivot_yaw_tolerance_) {
    if (std::abs(velocity.angular.z) >= transfer_angular_stop_) {
      return stopped(pose);
    }
    ++stage_index_;
    activateStage();
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
  return command;
}

void SegmentedRPPController::setSpeedLimit(
  const double & speed_limit, const bool & percentage)
{
  rpp_->setSpeedLimit(speed_limit, percentage);
}

}  // namespace patrol_navigation_controller

PLUGINLIB_EXPORT_CLASS(
  patrol_navigation_controller::SegmentedRPPController, nav2_core::Controller)
