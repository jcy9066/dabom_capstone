// Copyright (c) 2026. Licensed under MIT.
#ifndef PATROL_NAVIGATION_CONTROLLER__TIME_SCORED_LATTICE_PLANNER_HPP_
#define PATROL_NAVIGATION_CONTROLLER__TIME_SCORED_LATTICE_PLANNER_HPP_

#include <memory>
#include <string>
#include <vector>

#include "geometry_msgs/msg/pose_stamped.hpp"
#include "nav_msgs/msg/path.hpp"
#include "nav2_core/global_planner.hpp"
#include "nav2_costmap_2d/costmap_2d_ros.hpp"
#include "patrol_navigation_controller/path_time_estimator.hpp"
#include "pluginlib/class_loader.hpp"
#include "rclcpp_lifecycle/lifecycle_node.hpp"
#include "tf2_ros/buffer.h"

namespace patrol_navigation_controller
{

// One Nav2 GlobalPlanner ID, multiple collision-checked Smac lattice
// candidates. Select by estimated drive/pivot time rather than shortest XY.
class TimeScoredLatticePlanner : public nav2_core::GlobalPlanner
{
public:
  TimeScoredLatticePlanner();
  ~TimeScoredLatticePlanner() override = default;

  void configure(
    const rclcpp_lifecycle::LifecycleNode::WeakPtr & parent,
    std::string name, std::shared_ptr<tf2_ros::Buffer> tf,
    std::shared_ptr<nav2_costmap_2d::Costmap2DROS> costmap_ros) override;
  void cleanup() override;
  void activate() override;
  void deactivate() override;
  nav_msgs::msg::Path createPlan(
    const geometry_msgs::msg::PoseStamped & start,
    const geometry_msgs::msg::PoseStamped & goal) override;

private:
  pluginlib::ClassLoader<nav2_core::GlobalPlanner> loader_;
  std::vector<std::string> candidate_ids_;
  std::vector<nav2_core::GlobalPlanner::Ptr> candidates_;
  rclcpp_lifecycle::LifecycleNode::WeakPtr parent_;
  MotionTimeLimits time_limits_;
  double max_total_planning_sec_ = 5.0;
  std::string name_;
};

}  // namespace patrol_navigation_controller
#endif  // PATROL_NAVIGATION_CONTROLLER__TIME_SCORED_LATTICE_PLANNER_HPP_
