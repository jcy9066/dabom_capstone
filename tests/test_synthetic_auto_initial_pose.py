"""Offline synthetic LiDAR-to-map localization regressions; no robot required."""
import json
import math
import time
from pathlib import Path
from types import SimpleNamespace
import pytest

from server.auto_initial_pose import AutoPoseUncertain, estimate_initial_pose


def make_env(tmp_path, design="asymmetric", width=65, height=65, res=.10):
    occupied={(x, y) for x in range(width) for y in (0,height-1)} | {(x,y) for x in (0,width-1) for y in range(height)}
    if design == "asymmetric":
        occupied|={(x,y) for x in range(43,46) for y in range(14,33)}
        occupied|={(x,y) for x in range(12,28) for y in range(44,47)}
        occupied|={(x,y) for x in range(8,12) for y in range(13,22)}
        occupied|={(x,y) for x in range(52,57) for y in range(47,51)}
        occupied|={(x,y) for x in range(24,28) for y in range(8,12)}
    grid=[100 if (x,y) in occupied else 0 for y in range(height) for x in range(width)]
    path=tmp_path/'raw.json'
    path.write_text(json.dumps({'data': grid}),encoding='utf-8')
    selected=SimpleNamespace(raw_path=path,width=width,height=height,resolution=res,
                             origin_x=0.0,origin_y=0.0,origin_yaw=0.0)
    return selected,occupied


def synth_scan(env, occupied, pose=(3.1, 2.75, .7), now=1234, count=48):
    x,y,yaw=pose
    ranges=[]
    for i in range(count):
        alpha=-math.pi + i*2*math.pi/count
        direction=yaw+math.pi+alpha
        d=0.15
        while d<4.5:
            px=x+d*math.cos(direction);py=y+d*math.sin(direction)
            cx=int(px/env.resolution); cy=int(py/env.resolution)
            if (cx,cy) in occupied:
                break
            d+=0.02
        ranges.append(round(d, 3) if d<4.5 else float('inf'))
    return {"received_at":now, "angle_min":-math.pi,"angle_increment":2*math.pi/count,
            "range_min":0.12,"range_max":4.5,"ranges":ranges,
            "mount":{"x":0,"y":0,"yaw":math.pi}}


def test_unique_synthetic_room(tmp_path):
    env,occupied=make_env(tmp_path)
    truth=(3.1,2.75,.7)
    scan=synth_scan(env,occupied,truth)
    result=estimate_initial_pose(env,scan,now=1234.1)
    position_error=math.hypot(result['x']-truth[0],result['y']-truth[1])
    heading_error=abs(math.atan2(math.sin(math.radians(result['yaw_degrees'])-truth[2]),
                                 math.cos(math.radians(result['yaw_degrees'])-truth[2])))
    assert position_error <= 0.35
    assert heading_error <= .35


def test_rotated_obstacle_free_symmetric_room_requires_manual(tmp_path):
    env, occupied=make_env(tmp_path,design="rectangle")
    scan=synth_scan(env,occupied,pose=(3.25,3.25,0))
    with pytest.raises(AutoPoseUncertain):
        estimate_initial_pose(env,scan,now=1234.1)


def test_noisy_pose_matrix_does_not_approve_wrong_position(tmp_path):
    import random
    env,occupied=make_env(tmp_path)
    poses=[(3.1,2.75,.7),(3.2,2.6,-.5),(3.2,2.6,1.6),
           (1.5,3.1,.2),(2.9,3.55,0.8),(3.8,3.85,-1.0),
           (1.9,2.1,2.1),(5.25,3.7,.1),(4.5,4.0,1.3),
           (2.0,3.25,-2.3)]
    rng=random.Random(152)
    accepted=rejected=0
    for truth in poses:
        scan=synth_scan(env,occupied,truth)
        scan['ranges']=[(max(.12,dist+rng.gauss(0,.012)) if math.isfinite(dist) else dist)
                        if rng.random()>.04 else float('inf') for dist in scan['ranges']]
        try:
            result=estimate_initial_pose(env,scan,now=1234.1)
        except AutoPoseUncertain:
            rejected+=1
            continue
        accepted+=1
        pos=math.hypot(result['x']-truth[0], result['y']-truth[1])
        yaw=math.radians(result['yaw_degrees'])
        heading=abs(math.atan2(math.sin(yaw-truth[2]),math.cos(yaw-truth[2])))
        assert pos<.35 and heading<.35, (truth,result,pos,heading)
    assert accepted>=6
    assert accepted+rejected==len(poses)
