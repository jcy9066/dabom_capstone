import math
import pytest
from server.auto_initial_pose import AutoPoseUncertain
from server.scan_pose_fusion import fuse_stationary_scans, select_independent_encoder_scans
from server.amcl_pose_stability import stable_amcl_observations


def frames(n=5, moving=False):
    out = []
    for i in range(n):
        out.append({'scan': {'received_at': 99.0 + i * 0.3, 'angle_min':-math.pi,
                  'angle_increment': .1, 'range_min': .1, 'range_max': 8.0,
                  'ranges': [1.1 + (i % 3 - 1) * .01]*48,
                  'mount': {'x': 0, 'y': 0, 'yaw': math.pi}},
                  'wheel_ticks': (30+(i if moving else 0), 30, 30, 30),
                  'encoder_received_at': 98.95 + i*0.3,
                  'encoder_sequence': 100 + i})
    return out


def test_stationary_fusion():
    fused=fuse_stationary_scans(frames(), now=100.3)
    assert fused['frames_fused'] == 5
    assert fused['stable_beams'] == 48
    assert 1.08 < fused['ranges'][3] < 1.12


def test_moving_rejected():
    with pytest.raises(AutoPoseUncertain, match='movement'):
        fuse_stationary_scans(frames(moving=True), now=100.3)


def test_missing_encoder_rejected():
    f=frames()
    f[1].pop('wheel_ticks')
    with pytest.raises(AutoPoseUncertain, match='encoder'):
        fuse_stationary_scans(f, now=100.3)


def test_repeated_stale_frames_rejected():
    f=frames()
    f[2]['scan']['received_at']=f[1]['scan']['received_at']
    with pytest.raises(AutoPoseUncertain, match='timestamp'):
        fuse_stationary_scans(f, now=100.3)


def test_inconsistent_geometry_rejected():
    f=frames()
    f[2]['scan']['angle_increment']=0.12
    with pytest.raises(AutoPoseUncertain, match='geometry'):
        fuse_stationary_scans(f, now=100.3)


def test_dynamic_obstacle_returns_are_ignored():
    f=frames()
    f[0]['scan']['ranges'][12]=0.4
    fused=fuse_stationary_scans(f,now=100.3)
    assert fused['ranges'][12] == pytest.approx(1.1,abs=.02)


def test_amcl_stable():
    initial={'x':1.0,'y':2.0,'yaw':0.1}
    sample=[{'x':1.02,'y':2.0,'yaw':0.1,'at':t,'xy_variance':.05,'yaw_variance':.02} for t in (1.,1.2,1.4)]
    assert stable_amcl_observations(sample,initial)


def test_amcl_jump_and_covariance_fail():
    initial={'x':1.0,'y':2.0,'yaw':0.1}
    sample=[{'x':1.02,'y':2.0,'yaw':0.1,'at':t,'xy_variance':.05,'yaw_variance':.02} for t in (1.,1.2,1.4)]
    sample[1]['x']+=.31
    assert not stable_amcl_observations(sample, initial)
    sample[1]['x']-=.31
    sample[-1]['xy_variance']=1.0
    assert not stable_amcl_observations(sample,initial)

def test_fusion_feeds_existing_map_matcher(tmp_path):
    from test_synthetic_auto_initial_pose import make_env, synth_scan
    from server.auto_initial_pose import estimate_initial_pose
    env, obstacles = make_env(tmp_path)
    truth = (3.1, 2.75, .7)
    seed = synth_scan(env, obstacles, truth, now=200.0)
    f=[]
    for i in range(5):
        scan=dict(seed)
        scan['ranges']=list(seed['ranges'])
        scan['received_at']=200.0+i*.25
        f.append({'scan': scan, 'wheel_ticks': (100,100,100,100),
                  'encoder_received_at': scan['received_at']-.02,
                  'encoder_sequence': 500+i})
    fused=fuse_stationary_scans(f,now=201.1)
    pose=estimate_initial_pose(env, fused, now=201.1)
    assert math.hypot(pose['x']-truth[0], pose['y']-truth[1]) < .35
    yaw=math.radians(pose['yaw_degrees'])
    assert abs(math.atan2(math.sin(yaw-truth[2]),math.cos(yaw-truth[2]))) < .35

def test_amcl_non_monotonic_timestamps_rejected():
    initial={'x':1.0,'y':2.0,'yaw':0.1}
    data=[{'x':1.02,'y':2.0,'yaw':0.1,'at':t,'xy_variance':.05,'yaw_variance':.02}
          for t in (1.,1.0,1.4)]
    assert not stable_amcl_observations(data, initial)


def test_amcl_requires_three_post_initialpose_samples():
    initial={'x':1.0,'y':2.0,'yaw':0.1}
    data=[{'x':1.02,'y':2.0,'yaw':0.1,'at':t,'xy_variance':.05,'yaw_variance':.02}
          for t in (1.,1.4)]
    assert not stable_amcl_observations(data,initial)

def test_amcl_high_rate_still_converges():
    initial={'x':1.0,'y':2.0,'yaw':0.1}
    samples=[{'x':1.01,'y':2.0,'yaw':0.1,'at':1.0+i*.05,
              'xy_variance':.05,'yaw_variance':.02} for i in range(16)]
    assert stable_amcl_observations(samples,initial)


def test_duplicate_encoder_packet_does_not_prove_stationarity():
    sample = frames()
    sample[2]['encoder_sequence'] = sample[1]['encoder_sequence']
    with pytest.raises(AutoPoseUncertain, match="Repeated or out-of-order"):
        fuse_stationary_scans(sample, now=100.3)


def test_reused_encoder_timestamp_is_rejected():
    sample = frames()
    sample[2]['encoder_received_at'] = sample[1]['encoder_received_at']
    with pytest.raises(AutoPoseUncertain, match="Repeated or out-of-order"):
        fuse_stationary_scans(sample, now=100.3)


def test_missing_encoder_sequence_is_rejected():
    sample = frames()
    sample[2].pop('encoder_sequence')
    with pytest.raises(AutoPoseUncertain, match="sequence unavailable"):
        fuse_stationary_scans(sample, now=100.3)


def test_scan_faster_than_encoder_skips_duplicates():
    samples = frames()
    repeated = []
    for sample in samples:
        repeated.extend([sample, {**sample, 'scan': dict(sample['scan'])}])
    for i in range(0, len(repeated), 2):
        repeated[i+1]['scan']['received_at'] += .01
    selected = select_independent_encoder_scans(repeated)
    assert len(selected) == 5
    assert [s["encoder_sequence"] for s in selected] == list(range(100,105))
    assert fuse_stationary_scans(selected, now=100.3)['frames_fused'] == 5


def test_encoder_sequence_rollback_breaks_history():
    samples = frames()
    samples[3]['encoder_sequence'] = 5
    selected = select_independent_encoder_scans(samples)
    assert [s["encoder_sequence"] for s in selected] == [5,104]
    with pytest.raises(AutoPoseUncertain):
        fuse_stationary_scans(selected, now=100.3)


def test_amcl_100hz_observations_need_sufficient_history():
    initial = {'x': 1.0, 'y': 2.0, 'yaw': 0.1}
    samples = [
        {'x': 1.0, 'y': 2.0, 'yaw': 0.1, 'at': 1 + i * .01,
         'xy_variance': .05, 'yaw_variance': .02}
        for i in range(45)
    ]
    assert stable_amcl_observations(samples, initial)
    assert not stable_amcl_observations(samples[-20:], initial)


def test_amcl_100hz_still_rejects_outlier():
    initial = {'x': 1.0, 'y': 2.0, 'yaw': 0.1}
    samples = [
        {'x': 1.0, 'y': 2.0, 'yaw': 0.1, 'at': 1 + i * .01,
         'xy_variance': .05, 'yaw_variance': .02}
        for i in range(45)
    ]
    samples[30]['x'] += .4
    assert not stable_amcl_observations(samples, initial)
