import numpy as np


def process_topdown_many(analyzer, frame, objs, total_frames, pose_scope="mmpose"):
    from mmpose.apis import inference_topdown
    from mmengine.registry import DefaultScope

    results = {obj["id"]: (None, None) for obj in objs}
    valid_objs = [obj for obj in objs if obj.get("box") is not None]
    if not valid_objs:
        return results

    bboxes = np.array([list(map(int, obj["box"])) for obj in valid_objs])
    if pose_scope:
        with DefaultScope.overwrite_default_scope(pose_scope):
            pose_results = inference_topdown(analyzer.pose_model, frame, bboxes, bbox_format="xyxy")
    else:
        pose_results = inference_topdown(analyzer.pose_model, frame, bboxes, bbox_format="xyxy")

    for obj, pose_result in zip(valid_objs, pose_results):
        pred = getattr(pose_result, "pred_instances", None)
        if pred is None or len(pred.keypoints) == 0:
            continue
        kpts = pred.keypoints[0]
        scores = pred.keypoint_scores[0]
        results[obj["id"]] = _append_and_classify(analyzer, frame, obj, kpts, scores, total_frames)
    return results


def process_keypoint_many(analyzer, frame, objs, total_frames):
    results = {}
    for obj in objs:
        kpts = obj.get("keypoints")
        scores = obj.get("keypoints_scores")
        if kpts is None or len(kpts) == 0:
            results[obj["id"]] = (None, None)
            continue
        results[obj["id"]] = _append_and_classify(analyzer, frame, obj, kpts, scores, total_frames)
    return results


def _append_and_classify(analyzer, frame, obj, kpts, scores, total_frames):
    obj_id = obj["id"]
    if obj_id not in analyzer.action_buffer:
        analyzer.action_buffer[obj_id] = {"kpts": [], "scores": []}

    analyzer.action_buffer[obj_id]["kpts"].append(kpts)
    analyzer.action_buffer[obj_id]["scores"].append(scores)

    cur_kpts = analyzer.action_buffer[obj_id]["kpts"]
    cur_scores = analyzer.action_buffer[obj_id]["scores"]
    pad_len = total_frames - len(cur_kpts)

    pad_kpts = cur_kpts + [cur_kpts[-1]] * pad_len if pad_len > 0 else cur_kpts
    pad_scores = cur_scores + [cur_scores[-1]] * pad_len if pad_len > 0 else cur_scores
    object_classifier = getattr(analyzer, "_classify_with_object", None)
    if callable(object_classifier):
        action_res = object_classifier(obj_id, pad_kpts, pad_scores, frame.shape)
    else:
        action_res = analyzer._classify(pad_kpts, pad_scores, frame.shape)

    if len(analyzer.action_buffer[obj_id]["kpts"]) >= total_frames:
        analyzer.action_buffer[obj_id]["kpts"].pop(0)
        analyzer.action_buffer[obj_id]["scores"].pop(0)

    return kpts, action_res
