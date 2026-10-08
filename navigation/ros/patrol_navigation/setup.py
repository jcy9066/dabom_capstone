# ROS2 ament_python package용 Python packaging metadata다.
#
# colcon은 이 파일을 읽어서 Python node, launch 파일, config 파일, RViz 설정을 install 공간에 복사한다.

from glob import glob
from setuptools import find_packages, setup

package_name = "patrol_navigation"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        # ROS2가 이 package를 찾을 수 있게 해주는 필수 marker다.
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),

        # package manifest다.
        (f"share/{package_name}", ["package.xml"]),

        # Python 코드가 아닌 runtime asset들을 package share 디렉터리에 설치한다.
        (f"share/{package_name}/launch", glob("launch/*.launch.py")),
        (f"share/{package_name}/config", glob("config/*.yaml")),
        (f"share/{package_name}/behavior_trees", glob("behavior_trees/*.xml")),
        (f"share/{package_name}/rviz", glob("rviz/*.rviz")),
        (f"share/{package_name}/maps", glob("../../maps/slam_test_01.*")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="cwjeong",
    maintainer_email="cwjeong@example.com",
    description="ROS 2 mapping bridge and launch files for the AI patrol robot.",
    license="MIT",
    entry_points={
        "console_scripts": [
            # launch 파일에서 실행하는 `map_bridge` executable을 만든다.
            "map_bridge = patrol_navigation.map_bridge:main",

            # Nav2의 Twist를 좌우 바퀴 속도로 변환하고
            # 서버 dry-run API에 전달한다.
            (
                "nav2_command_bridge = "
                "patrol_navigation.nav2_command_bridge:main"
            ),
        ],
    },
)
