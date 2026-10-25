import math
from collections import deque

class CascadingTrigger:
    def __init__(self):
        self.history = {}
        # 1. 형태 변화: 기존 1.2 -> 1.1로 낮춤 (몸이 조금만 가로로 기울어도 즉시 의심)
        self.aspect_ratio_threshold = 1.1 
        # 2. 이동 속도: 기존 30.0 -> 15.0으로 낮춤 (갑자기 발걸음만 빨라져도 즉시 의심)
        self.velocity_threshold = 15.0     
        # 3. 위험 반경: 기존 150 -> 250 픽셀로 대폭 확대
        self.proximity_threshold = 250     

    def get_object_states(self, tracked_boxes):
        """
        반환값 - 0: Normal (렌더링 안 함), 1: Suspicious (주황색, 행동 분석 시작)
        """
        persons = [obj for obj in tracked_boxes if obj['cls'] == 0]
        states = {obj['id']: 0 for obj in persons}

        # 사람에 대한 속도, 형태, 밀집도 분석
        for p in persons:
            oid = p['id']
            cx, cy = p['center']
            x1, y1, x2, y2 = p['box']
            width = x2 - x1
            height = max(y2 - y1, 1)
            
            if oid not in self.history:
                self.history[oid] = deque(maxlen=5)
            self.history[oid].append((cx, cy))

            # 조건 A: 몸의 비율이 가로로 변형됨 (몸싸움, 넘어짐 등)
            if (width / height) > self.aspect_ratio_threshold:
                states[oid] = 1

            # 조건 B: 속도 급증 (뛰어가거나 달려드는 행위)
            if len(self.history[oid]) >= 2:
                prev_cx, prev_cy = self.history[oid][-2]
                velocity = math.hypot(cx - prev_cx, cy - prev_cy)
                if velocity > self.velocity_threshold:
                    states[oid] = 1

        # 조건 C: 두 사람이 비정상적으로 가까워질 때 (125픽셀 이내 접근)
        for i, p1 in enumerate(persons):
            for p2 in persons[i+1:]:
                dist = math.hypot(p1['center'][0] - p2['center'][0], p1['center'][1] - p2['center'][1])
                if dist < (self.proximity_threshold / 2): 
                    states[p1['id']] = 1
                    states[p2['id']] = 1

        return states