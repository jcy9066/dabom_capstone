// Copyright (c) 2026. Licensed under MIT.
#ifndef PATROL_NAVIGATION_CONTROLLER__SEGMENTED_RPP_CONTROLLER_HPP_
#define PATROL_NAVIGATION_CONTROLLER__SEGMENTED_RPP_CONTROLLER_HPP_

#include <cstddef>
#include <memory>
#include <string>
#include <vector>

#include "geometry_msgs/msg/pose_stamped.hpp"
#include "geometry_msgs/msg/twist.hpp"
#include "geometry_msgs/msg/twist_stamped.hpp"
#include "nav_msgs/msg/path.hpp"
#include "nav2_core/controller.hpp"
#include "nav2_costmap_2d/costmap_2d_ros.hpp"
#include "nav2_costmap_2d/footprint_collision_checker.hpp"
#include "pluginlib/class_loader.hpp"
#include "rclcpp_lifecycle/lifecycle_node.hpp"
#include "tf2_ros/buffer.h"

namespace patrol_navigation_controller
{

class SegmentedRPPController : public nav2_core::Controller
{
public:
  SegmentedRPPController();
  ~SegmentedRPPController() override = default;

  void configure(
    const rclcpp_lifecycle::LifecycleNode::WeakPtr & parent,
    std::string name, std::shared_ptr<tf2_ros::Buffer> tf,
    std::shared_ptr<nav2_costmap_2d::Costmap2DROS> costmap_ros) override;
  void cleanup() override;
  void activate() override;
  void deactivate() override;
  void setPlan(const nav_msgs::msg::Path & path) override;
  geometry_msgs::msg::TwistStamped computeVelocityCommands(
    const geometry_msgs::msg::PoseStamped & pose,
    const geometry_msgs::msg::Twist & velocity,
    nav2_core::GoalChecker * goal_checker) override;
  void setSpeedLimit(const double & speed_limit, const bool & percentage) override;

private:
  enum class Mode { Drive, Pivot };
  struct Stage
  {
    Mode mode;
    size_t begin;
    size_t end;  // Inclusive end point in original Path
    double target_yaw = 0.0;
  };

  static double yaw(const geometry_msgs::msg::PoseStamped & pose);
  static double distance(
    const geometry_msgs::msg::PoseStamped & first,
    const geometry_msgs::msg::PoseStamped & second);
  geometry_msgs::msg::PoseStamped inControllerFrame(
    const geometry_msgs::msg::PoseStamped & target,
    const std::string & target_frame) const;
  void buildStages();
  void activateStage();
  void advanceStage();
  void invalidatePlan() noexcept;
  void checkRotationCollision(
    const geometry_msgs::msg::PoseStamped & pose,
    double angle_error);
  geometry_msgs::msg::TwistStamped stopped(
    const geometry_msgs::msg::PoseStamped & pose) const;
  geometry_msgs::msg::TwistStamped checkedCommand(
    const geometry_msgs::msg::PoseStamped & pose,
    const geometry_msgs::msg::TwistStamped & command);

  pluginlib::ClassLoader<nav2_core::Controller> rpp_loader_;
  nav2_core::Controller::Ptr rpp_;
  rclcpp_lifecycle::LifecycleNode::WeakPtr parent_;
  std::shared_ptr<tf2_ros::Buffer> tf_;
  std::shared_ptr<nav2_costmap_2d::Costmap2DROS> costmap_;
  std::unique_ptr<nav2_costmap_2d::FootprintCollisionChecker<
    nav2_costmap_2d::Costmap2D *>> collision_;
  std::string plugin_name_;
  nav_msgs::msg::Path path_;
  std::vector<Stage> stages_;
  size_t stage_index_ = 0;
  // Fail closed if replacement planning or a handoff cannot initialize.
  // A nonempty old inner-RPP path must never authorize stale driving.
  bool plan_ready_ = false;
  bool pivot_started_ = false;
  // Loop-only controller rejects any reverse-drive segment instead of
  // silently backing up when the planner cannot provide a pivot route.
  bool loop_forward_only_ = false;
  // Loop half-turn ambiguity (pi == -pi): prefer clockwise first, then
  // counterclockwise after a physically completed turn. Never toggle on
  // a rejected path, a safety STOP, or an incomplete pivot.
  bool loop_turn_clockwise_ = true;
  bool loop_half_turn_in_progress_ = false;
  double controller_frequency_ = 10.0;
  double pivot_max_angular_ = 1.05;
  double pivot_acceleration_ = 1.0;
  double pivot_yaw_tolerance_ = 0.12;
  double transfer_position_tolerance_ = 0.08;
  double transfer_linear_stop_ = 0.02;
  double transfer_angular_stop_ = 0.04;
};

}  // namespace patrol_navigation_controller

#endif  // PATROL_NAVIGATION_CONTROLLER__SEGMENTED_RPP_CONTROLLER_HPP_
