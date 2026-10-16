cmake_minimum_required(VERSION 3.8)
project(patrol_navigation_controller)

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclcpp_lifecycle REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(nav_msgs REQUIRED)
find_package(tf2 REQUIRED)
find_package(tf2_ros REQUIRED)
find_package(angles REQUIRED)
find_package(pluginlib REQUIRED)
find_package(nav2_core REQUIRED)
find_package(nav2_costmap_2d REQUIRED)
find_package(nav2_util REQUIRED)

add_library(segmented_rpp_controller SHARED
  src/segmented_rpp_controller.cpp
  src/time_scored_lattice_planner.cpp
)
target_compile_features(segmented_rpp_controller PUBLIC cxx_std_17)
target_include_directories(segmented_rpp_controller PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>
)
ament_target_dependencies(segmented_rpp_controller
  rclcpp rclcpp_lifecycle geometry_msgs nav_msgs tf2 tf2_ros
  angles pluginlib nav2_core nav2_costmap_2d nav2_util
)
pluginlib_export_plugin_description_file(nav2_core plugins.xml)

install(TARGETS segmented_rpp_controller
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION bin
)
install(DIRECTORY include/ DESTINATION include)
ament_export_include_directories(include)
ament_export_libraries(segmented_rpp_controller)
ament_export_dependencies(
  rclcpp rclcpp_lifecycle geometry_msgs nav_msgs tf2 tf2_ros
  angles pluginlib nav2_core nav2_costmap_2d nav2_util
)
ament_package()
