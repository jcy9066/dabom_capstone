"""Structural regression of Nav2 Humble's conditional Global Path BT.

Runtime BT execution (Nav2/Humble) is still required before merge. These
checks prevent reintroducing uninitialized IsPathValid, unconditional rate
replanning, or a debounce that ignores a recovered path.
"""
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
NAV = ROOT / "navigation/ros/patrol_navigation"
BT = NAV / "behavior_trees/navigate_to_pose_dynamic_replanning.xml"


def _root():
    document = ET.parse(BT)
    assert document.getroot().attrib["main_tree_to_execute"] == "MainTree"
    tree = document.find("./BehaviorTree[@ID='MainTree']")
    assert tree is not None
    return tree


def test_initial_compute_path_is_before_any_path_validity_check():
    tree = _root()
    recovery = tree.find("./RecoveryNode")
    assert recovery is not None
    assert recovery.attrib["number_of_retries"] == "5"
    sequence = recovery.find("./Sequence")
    assert sequence is not None
    assert sequence[0].tag == "ComputePathToPose"
    assert sequence[0].attrib["path"] == "{path}"
    assert sequence[1].tag == "PipelineSequence"


def test_valid_path_does_not_trigger_periodic_compute_path():
    tree = _root()
    rate = tree.find(".//PipelineSequence/RateController")
    assert rate is not None
    assert float(rate.attrib["hz"]) == 10.0
    fallback = rate.find("./Fallback")
    assert fallback is not None
    assert fallback[0].tag == "IsPathValid"
    assert fallback[1].tag == "ReactiveSequence"
    # The initial planning action is not a child of the monitoring rate:
    # valid paths pass through the first fallback child unchanged.
    assert len(fallback.findall("./ComputePathToPose")) == 0


def test_invalidity_is_sampled_continuously_across_debounce():
    reactive = _root().find(".//ReactiveSequence")
    assert reactive is not None
    assert reactive[0].tag == "Inverter"
    assert reactive[0][0].tag == "IsPathValid"
    delay = reactive[1]
    assert delay.tag == "Delay"
    assert int(delay.attrib["delay_msec"]) == 300
    assert len(delay) == 1 and delay[0].tag == "ComputePathToPose"
    assert delay[0].attrib["planner_id"] == "GridBased"


def test_no_blind_automatic_spin_or_reverse_recovery():
    tree = _root()
    assert tree.find(".//Spin") is None
    assert tree.find(".//BackUp") is None
    assert tree.find(".//DriveOnHeading") is None
    assert tree.find(".//ClearEntireCostmap") is None
    # A failed FollowPath must never be magically reported as success.
    follow = tree.find(".//PipelineSequence/FollowPath")
    assert follow is not None
    assert follow.attrib["controller_id"] == "FollowPath"


def test_navigator_uses_the_custom_bt_and_humble_nodes():
    launch = (NAV / "launch/navigation.launch.py").read_text()
    assert '"default_nav_to_pose_bt_xml": navigate_to_pose_bt' in launch
    params = (NAV / "config/nav2_params.yaml").read_text()
    assert "nav2_is_path_valid_condition_bt_node" in params
    assert "nav2_rate_controller_bt_node" in params
    assert "nav2_pipeline_sequence_bt_node" in params
