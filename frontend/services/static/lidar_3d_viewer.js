import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

const root = document.getElementById('lidar-3d-viewer');
const canvas = document.getElementById('lidar-3d-canvas');

if (root && canvas) {
    const COLORS = {
        background: 0x101821,
        mapFree: [62, 76, 88, 255],
        mapUnknown: [30, 40, 51, 255],
        mapOccupied: [86, 104, 118, 255],
        occupiedWall: 0x70869a,
        lowerChassis: 0x667582,
        upperChassis: 0x98a6b1,
        wheel: 0x222a31,
        robotFront: 0x5b9bd5,
        scanRay: 0x7dd3fc,
        scanPoint: 0x2dd4bf,
        globalPath: 0x8b5cf6,
        trajectory: 0xf59e0b,
        goal: 0xfb7185,
        draftGoal: 0xfbbf24,
        cameraBody: 0x303941,
        cameraLens: 0x111827,
        cameraFrustum: 0xf59aa8,
        cameraViewBorder: 0xe2e8f0,
    };

    const DEFAULT_OCCUPIED_HEIGHT_M = 0.08;
    const MAP_OCCUPIED_THRESHOLD = 50;
    const MAPPING_DYNAMIC_MAP_REFRESH_MS = 1000;
    const LIDAR_HEIGHT_M = 0.12;
    // The physical LiDAR is mounted 180° relative to the 3D viewer's +X heading.
    // Keep map/pose/navigation coordinates unchanged and rotate only scan visuals.
    const LIDAR_VISUAL_YAW_OFFSET_RAD = Math.PI;
    const MAPPING_PREVIEW_POSE = Object.freeze({ x: 0, y: 0, yaw: 0 });
    // Visual pose smoothing follows differential-drive motion: forward motion responds
    // faster than lateral map/localization correction so the chassis does not appear
    // to slide sideways across its fixed wheel direction.
    const POSE_FORWARD_RESPONSE_PER_SEC = 7.0;
    const POSE_LATERAL_RESPONSE_PER_SEC = 2.5;
    const POSE_YAW_RESPONSE_PER_SEC = 9.0;
    const MAX_VISUAL_TELEPORT_M = 1.5;
    const TRAJECTORY_DISTANCE_M = 0.08;
    const TRAJECTORY_FALLBACK_DISTANCE_M = 0.02;
    const TRAJECTORY_FALLBACK_MS = 500;
    const MAX_TRAJECTORY_POINTS = 4000;
    // Visualization-only camera geometry. The stream aspect ratio stays native.
    const CAMERA_VIEW_DISTANCE_M = 0.75;
    const CAMERA_VIEW_HEIGHT_M = 0.34;
    const CAMERA_TEXTURE_MAX_WIDTH = 512;
    const CAMERA_TEXTURE_FPS = 12;
    const CAMERA_MOUNT_FORWARD_RATIO = 0.44;
    const ORIGINAL_CAMERA_HEIGHT_OFFSET_M = 0.03;

    const renderer = new THREE.WebGLRenderer({
        canvas,
        antialias: true,
        alpha: false,
        powerPreference: 'high-performance',
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.25));
    renderer.setClearColor(COLORS.background, 1);
    renderer.outputColorSpace = THREE.SRGBColorSpace;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(COLORS.background);

    const camera = new THREE.PerspectiveCamera(48, 1, 0.02, 200);
    camera.position.set(4.8, -5.8, 5.2);
    camera.up.set(0, 0, 1);

    const controls = new OrbitControls(camera, canvas);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.enablePan = true;
    controls.enableRotate = true;
    controls.enableZoom = true;
    controls.screenSpacePanning = true;
    controls.minDistance = 0.45;
    controls.maxDistance = 40;
    controls.maxPolarAngle = Math.PI * 0.495;
    controls.target.set(0, 0, 0);
    controls.mouseButtons.LEFT = THREE.MOUSE.ROTATE;
    controls.mouseButtons.RIGHT = THREE.MOUSE.PAN;
    controls.mouseButtons.MIDDLE = THREE.MOUSE.DOLLY;

    const world = new THREE.Group();
    world.name = 'navigation-world';
    scene.add(world);

    const layerGroups = {};
    for (const key of [
        'map',
        'robot',
        'grid',
        'tf',
        'scan',
        'points',
        'path',
        'trajectory',
        'goal',
        'camera-frustum',
        'camera-view',
    ]) {
        const group = new THREE.Group();
        group.name = key;
        layerGroups[key] = group;
        world.add(group);
    }

    const ambient = new THREE.HemisphereLight(0xd8e5ef, 0x1d2832, 1.05);
    scene.add(ambient);

    const keyLight = new THREE.DirectionalLight(0xf2f7fa, 1.15);
    keyLight.position.set(3, -4, 7);
    scene.add(keyLight);

    const mapRoot = new THREE.Group();
    mapRoot.name = 'occupancy-map';
    layerGroups.map.add(mapRoot);

    const gridRoot = new THREE.Group();
    gridRoot.name = 'map-grid';
    layerGroups.grid.add(gridRoot);

    const robotPoseGroup = new THREE.Group();
    robotPoseGroup.name = 'robot-base';
    robotPoseGroup.visible = false;
    layerGroups.robot.add(robotPoseGroup);

    const tfPoseGroup = new THREE.Group();
    tfPoseGroup.name = 'base-tf';
    tfPoseGroup.visible = false;
    layerGroups.tf.add(tfPoseGroup);

    // TF layer is independent from the robot model and follows the raw
    // map->base_link transform without visual smoothing, like RViz2 TF.
    let baseAxes = new THREE.AxesHelper(0.18);
    baseAxes.position.z = 0.01;
    tfPoseGroup.add(baseAxes);

    const scanRoot = new THREE.Group();
    scanRoot.name = 'laser-scan-rays';
    layerGroups.scan.add(scanRoot);

    const pointsRoot = new THREE.Group();
    pointsRoot.name = 'lidar-points';
    layerGroups.points.add(pointsRoot);

    const pathRoot = new THREE.Group();
    pathRoot.name = 'global-path';
    layerGroups.path.add(pathRoot);

    const trajectoryRoot = new THREE.Group();
    trajectoryRoot.name = 'driving-trajectory';
    trajectoryRoot.visible = false;
    layerGroups.trajectory.add(trajectoryRoot);

    const goalRoot = new THREE.Group();
    goalRoot.name = 'navigation-goals';
    layerGroups.goal.add(goalRoot);

    const cameraFrustumPoseGroup = new THREE.Group();
    cameraFrustumPoseGroup.name = 'camera-frustum-pose';
    cameraFrustumPoseGroup.visible = false;
    layerGroups['camera-frustum'].add(cameraFrustumPoseGroup);

    const cameraViewPoseGroup = new THREE.Group();
    cameraViewPoseGroup.name = 'camera-view-pose';
    cameraViewPoseGroup.visible = false;
    layerGroups['camera-view'].add(cameraViewPoseGroup);

    let currentMapKey = null;
    let currentBaseMapKey = null;
    let currentMap = null;
    let currentMapRevision = null;
    let currentBaseMapCells = null;
    let mappingOverlayScan = null;
    let mappingOverlayPose = null;
    let mappingOverlayKey = null;
    let obstacleHeightM = DEFAULT_OCCUPIED_HEIGHT_M;
    let robotModelReady = false;
    let robotVisual = null;
    const wheelVisuals = { left: [], right: [] };
    let wheelVisualRadiusM = 0;
    let wheelVisualTrackM = 0;
    const wheelRollRadians = { left: 0, right: 0 };
    let viewMode = 'top';
    let expandedViewMode = 'free';
    let viewerExpanded = root.closest('.minimap-overlay')?.classList.contains('expanded') === true;
    let interactionMode = 'view';
    let followTarget = null;
    let currentVisualizationState = null;
    let currentControlState = null;
    let currentDraftGoal = null;
    let previousNavigationMode = null;
    let previousNavigationState = null;
    let trajectorySessionActive = false;
    let trajectorySamples = [];
    let lastTrajectorySampleAt = 0;
    let cameraVisualReady = false;
    let cameraMountLocal = new THREE.Vector3(0.12, 0, LIDAR_HEIGHT_M);
    let scanVisualHeightM = 0.16;
    let cameraAspect = 4 / 3;
    let cameraViewPlane = null;
    let cameraViewBorder = null;
    let cameraFrustumLines = null;
    let cameraTexture = null;
    let cameraTextureCanvas = null;
    let cameraTextureContext = null;
    let lastCameraTextureAt = 0;
    let targetPose = null;
    let renderedPose = null;
    let lastAnimationAt = 0;
    let currentScanKey = null;
    let scanRayGeometry = null;
    let scanPointGeometry = null;
    let scanRayPositions = null;
    let scanPointPositions = null;
    let scanCapacity = 0;
    let currentPathKey = null;
    let currentGoalKey = null;
    let trajectoryGeometry = null;
    let trajectoryPositions = null;

    const cameraImage = document.getElementById('camera-stream');
    const controlDrawer = document.getElementById('lidar-control-drawer');
    const controlDrawerToggle = document.getElementById('lidarControlDrawerToggle');
    const layerDrawer = document.getElementById('lidar-layer-drawer');
    const layerDrawerToggle = document.getElementById('lidarLayerDrawerToggle');
    const viewerOverlay = root.closest('.minimap-overlay');
    const controlStrip = root.querySelector('.lidar-control-strip');
    const controlOverflow = document.getElementById('lidarControlOverflow');
    const controlOverflowToggle = document.getElementById('lidarControlOverflowToggle');
    const controlOverflowMenu = document.getElementById('lidar-control-overflow-menu');
    const overflowCandidates = [];
    let overflowLayoutFrame = 0;
    let controlOverflowExpanded = false;
    const raycaster = new THREE.Raycaster();
    const groundPlane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);

    if (controlStrip && controlOverflowMenu) {
        controlStrip.querySelectorAll('[data-control-overflow-rank]').forEach(element => {
            const placeholder = document.createComment('lidar-control-overflow-slot');
            element.parentNode?.insertBefore(placeholder, element);
            overflowCandidates.push({
                element,
                placeholder,
                rank: Number(element.dataset.controlOverflowRank) || 0,
            });
        });
        overflowCandidates.sort((a, b) => b.rank - a.rank);
    }

    function restoreOverflowCandidates() {
        for (const item of overflowCandidates) {
            const parent = item.placeholder.parentNode;
            if (!parent) continue;
            parent.insertBefore(item.element, item.placeholder.nextSibling);
        }
    }

    function setControlOverflowOpen(open) {
        if (!controlOverflow || !controlOverflowToggle || !controlOverflowMenu) return;
        const isOpen = Boolean(open)
            && !controlOverflow.hidden
            && controlOverflowMenu.children.length > 0;

        controlOverflowExpanded = isOpen;
        controlOverflowToggle.hidden = isOpen;
        controlOverflowToggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
        controlOverflow.classList.toggle('is-open', isOpen);
        controlOverflowMenu.hidden = !isOpen;

        if (isOpen) {
            requestAnimationFrame(() => {
                controlStrip?.scrollTo?.({
                    left: controlStrip.scrollWidth,
                    behavior: 'smooth',
                });
            });
        }
    }

    function layoutControlOverflow() {
        if (!controlStrip || !controlOverflow || !controlOverflowMenu) return;

        const keepExpanded = controlOverflowExpanded;
        restoreOverflowCandidates();
        controlOverflow.hidden = true;
        controlOverflowToggle.hidden = false;
        controlOverflowToggle.setAttribute('aria-expanded', 'false');
        controlOverflow.classList.remove('is-open');
        controlOverflowMenu.hidden = true;

        if (!viewerExpanded || controlDrawer?.dataset.open === 'false') {
            controlOverflowExpanded = false;
            return;
        }

        const overflows = () => (
            controlStrip.scrollWidth > controlStrip.clientWidth + 1
        );
        if (!overflows()) {
            controlOverflowExpanded = false;
            return;
        }

        controlOverflow.hidden = false;
        for (const item of overflowCandidates) {
            controlOverflowMenu.append(item.element);
            if (!overflows()) break;
        }

        if (!controlOverflowMenu.children.length) {
            controlOverflow.hidden = true;
            controlOverflowExpanded = false;
            return;
        }

        if (keepExpanded) {
            controlOverflowExpanded = true;
            controlOverflowToggle.hidden = true;
            controlOverflowToggle.setAttribute('aria-expanded', 'true');
            controlOverflow.classList.add('is-open');
            controlOverflowMenu.hidden = false;
        }
    }

    function scheduleControlOverflowLayout() {
        if (overflowLayoutFrame) return;
        overflowLayoutFrame = requestAnimationFrame(() => {
            overflowLayoutFrame = 0;
            layoutControlOverflow();
        });
    }

    function disposeMaterial(material) {
        if (!material) return;
        if (Array.isArray(material)) {
            material.forEach(disposeMaterial);
            return;
        }
        if (material.map) material.map.dispose();
        material.dispose?.();
    }

    function clearGroup(group) {
        while (group.children.length) {
            const child = group.children.pop();
            child.traverse?.(object => {
                object.geometry?.dispose?.();
                disposeMaterial(object.material);
            });
        }
    }

    function decodeRleMap(runs, expectedLength) {
        const output = new Int16Array(expectedLength);
        output.fill(-1);
        if (!Array.isArray(runs)) return output;

        let index = 0;
        for (const run of runs) {
            if (!Array.isArray(run) || run.length < 2) continue;
            const value = Number(run[0]);
            const count = Math.max(0, Number(run[1]) || 0);
            for (let offset = 0; offset < count && index < expectedLength; offset += 1) {
                output[index] = value;
                index += 1;
            }
            if (index >= expectedLength) break;
        }
        return output;
    }

    function mapKey(map, revision) {
        if (!map) return null;
        return [
            revision ?? '',
            map.timestamp || '',
            map.received_at || '',
            map.width || 0,
            map.height || 0,
            map.resolution || 0,
            obstacleHeightM.toFixed(4),
        ].join(':');
    }

    function mapTransform(map) {
        const origin = map?.origin || {};
        return {
            x: Number(origin.x) || 0,
            y: Number(origin.y) || 0,
            z: Number(origin.z) || 0,
            yaw: Number(origin.yaw) || 0,
        };
    }

    function mapWorldCenter(map) {
        if (!map) return null;
        const widthM = (Number(map.width) || 0) * (Number(map.resolution) || 0);
        const heightM = (Number(map.height) || 0) * (Number(map.resolution) || 0);
        if (widthM <= 0 || heightM <= 0) return null;

        const transform = mapTransform(map);
        const localX = widthM / 2;
        const localY = heightM / 2;
        const cosYaw = Math.cos(transform.yaw);
        const sinYaw = Math.sin(transform.yaw);
        return {
            x: transform.x + cosYaw * localX - sinYaw * localY,
            y: transform.y + sinYaw * localX + cosYaw * localY,
            z: transform.z,
            widthM,
            heightM,
        };
    }

    function applyCollapsedTopView() {
        const mapCenter = mapWorldCenter(currentMap);
        const target = mapCenter
            ? new THREE.Vector3(mapCenter.x, mapCenter.y, mapCenter.z)
            : (followTarget?.clone() || controls.target.clone());

        let distance = 4;
        if (mapCenter) {
            const verticalFov = THREE.MathUtils.degToRad(camera.fov);
            const horizontalFov = 2 * Math.atan(
                Math.tan(verticalFov / 2) * Math.max(camera.aspect, 0.1),
            );
            const verticalDistance = mapCenter.heightM / (2 * Math.tan(verticalFov / 2));
            const horizontalDistance = mapCenter.widthM / (2 * Math.tan(horizontalFov / 2));
            distance = Math.max(1.5, verticalDistance, horizontalDistance) * 1.12;
        }

        controls.target.copy(target);
        camera.up.set(0, 1, 0);
        camera.position.set(target.x, target.y, target.z + distance);
        camera.lookAt(target);
        controls.update();
    }

    function rebuildGrid(map) {
        clearGroup(gridRoot);
        if (!map) return;

        const width = Number(map.width) || 0;
        const height = Number(map.height) || 0;
        const resolution = Number(map.resolution) || 0;
        if (width <= 0 || height <= 0 || resolution <= 0) return;

        const widthM = width * resolution;
        const heightM = height * resolution;
        const size = Math.max(1, Math.ceil(Math.max(widthM, heightM)));
        const divisions = Math.min(100, Math.max(4, size * 2));
        const helper = new THREE.GridHelper(size, divisions, 0x64748b, 0x334155);
        helper.material.transparent = true;
        helper.material.opacity = 0.3;
        helper.material.depthWrite = false;
        helper.rotation.x = Math.PI / 2;
        helper.position.set(widthM / 2, heightM / 2, 0.001);
        gridRoot.add(helper);

        const transform = mapTransform(map);
        gridRoot.position.set(transform.x, transform.y, transform.z);
        gridRoot.rotation.z = transform.yaw;
    }

    function renderMapCells(map, cells, renderKey) {
        if (renderKey && renderKey === currentMapKey) return;

        clearGroup(mapRoot);
        currentMapKey = renderKey;

        if (!map || !cells) {
            clearGroup(gridRoot);
            return;
        }

        const width = Number(map.width) || 0;
        const height = Number(map.height) || 0;
        const resolution = Number(map.resolution) || 0;
        if (width <= 0 || height <= 0 || resolution <= 0) return;

        const rgba = new Uint8Array(width * height * 4);
        let occupiedCount = 0;

        for (let index = 0; index < cells.length; index += 1) {
            const value = cells[index];
            const color = value < 0
                ? COLORS.mapUnknown
                : (value >= MAP_OCCUPIED_THRESHOLD ? COLORS.mapOccupied : COLORS.mapFree);
            const offset = index * 4;
            rgba[offset] = color[0];
            rgba[offset + 1] = color[1];
            rgba[offset + 2] = color[2];
            rgba[offset + 3] = color[3];
            if (value >= MAP_OCCUPIED_THRESHOLD) occupiedCount += 1;
        }

        const texture = new THREE.DataTexture(rgba, width, height, THREE.RGBAFormat);
        texture.colorSpace = THREE.SRGBColorSpace;
        texture.magFilter = THREE.NearestFilter;
        texture.minFilter = THREE.NearestFilter;
        texture.generateMipmaps = false;
        texture.needsUpdate = true;

        const widthM = width * resolution;
        const heightM = height * resolution;
        const floor = new THREE.Mesh(
            new THREE.PlaneGeometry(widthM, heightM),
            new THREE.MeshBasicMaterial({
                map: texture,
                side: THREE.DoubleSide,
            }),
        );
        floor.position.set(widthM / 2, heightM / 2, 0);
        floor.name = 'occupancy-floor';
        mapRoot.add(floor);

        if (occupiedCount > 0) {
            const boxGeometry = new THREE.BoxGeometry(
                resolution * 0.94,
                resolution * 0.94,
                obstacleHeightM,
            );
            const boxMaterial = new THREE.MeshStandardMaterial({
                color: COLORS.occupiedWall,
                roughness: 0.88,
                metalness: 0.02,
            });
            const occupied = new THREE.InstancedMesh(
                boxGeometry,
                boxMaterial,
                occupiedCount,
            );
            occupied.name = 'occupied-cells';
            occupied.instanceMatrix.setUsage(THREE.DynamicDrawUsage);

            const matrix = new THREE.Matrix4();
            let instance = 0;
            for (let index = 0; index < cells.length; index += 1) {
                if (cells[index] < MAP_OCCUPIED_THRESHOLD) continue;
                const x = index % width;
                const y = Math.floor(index / width);
                matrix.makeTranslation(
                    (x + 0.5) * resolution,
                    (y + 0.5) * resolution,
                    obstacleHeightM / 2,
                );
                occupied.setMatrixAt(instance, matrix);
                instance += 1;
            }
            occupied.instanceMatrix.needsUpdate = true;
            mapRoot.add(occupied);
        }

        const transform = mapTransform(map);
        mapRoot.position.set(transform.x, transform.y, transform.z);
        mapRoot.rotation.z = transform.yaw;
        rebuildGrid(map);
        if (!viewerExpanded) applyCollapsedTopView();
    }

    function mapWorldToGrid(map, worldX, worldY) {
        const resolution = Number(map?.resolution) || 0;
        if (resolution <= 0) return null;

        const transform = mapTransform(map);
        const dx = worldX - transform.x;
        const dy = worldY - transform.y;
        const cosYaw = Math.cos(transform.yaw);
        const sinYaw = Math.sin(transform.yaw);

        // Inverse of map origin transform: world -> occupancy-grid local.
        const localX = cosYaw * dx + sinYaw * dy;
        const localY = -sinYaw * dx + cosYaw * dy;
        return {
            x: Math.floor(localX / resolution),
            y: Math.floor(localY / resolution),
        };
    }

    function setCompositeCell(cells, width, height, x, y, value) {
        if (x < 0 || y < 0 || x >= width || y >= height) return;
        cells[y * width + x] = value;
    }

    function raytraceComposite(
        cells,
        width,
        height,
        startCell,
        endCell,
        endpointOccupied,
    ) {
        if (!startCell || !endCell) return;

        let x0 = startCell.x;
        let y0 = startCell.y;
        const x1 = endCell.x;
        const y1 = endCell.y;
        const dx = Math.abs(x1 - x0);
        const dy = Math.abs(y1 - y0);
        const sx = x0 < x1 ? 1 : -1;
        const sy = y0 < y1 ? 1 : -1;
        let error = dx - dy;
        let steps = 0;
        const maxSteps = Math.max(width, height) * 4;

        while (steps < maxSteps) {
            if (x0 === x1 && y0 === y1) break;
            setCompositeCell(cells, width, height, x0, y0, 0);

            const doubled = error * 2;
            if (doubled > -dy) {
                error -= dy;
                x0 += sx;
            }
            if (doubled < dx) {
                error += dx;
                y0 += sy;
            }
            steps += 1;
        }

        if (endpointOccupied) {
            setCompositeCell(cells, width, height, x1, y1, 100);
        }
    }

    function composeMappingCells(map, baseCells, scan, pose) {
        if (
            !map
            || !baseCells
            || !validPose(pose)
            || !scan
            || !Array.isArray(scan.ranges)
            || !scan.ranges.length
        ) return null;

        const width = Number(map.width) || 0;
        const height = Number(map.height) || 0;
        if (width <= 0 || height <= 0) return null;

        // Start from the complete SLAM OccupancyGrid. Only cells observed by the
        // latest 1-second Mapping sample are overridden; every other mapped cell
        // remains exactly as slam_toolbox produced it.
        const cells = new Int16Array(baseCells);
        const angleMin = Number(scan.angle_min) || 0;
        const angleIncrement = Number(scan.angle_increment) || 0;
        const rangeMin = Math.max(0, Number(scan.range_min) || 0);
        const rangeMax = Number.isFinite(Number(scan.range_max))
            ? Number(scan.range_max)
            : 12;
        const robotX = Number(pose.x) || 0;
        const robotY = Number(pose.y) || 0;
        const sensorYaw = (Number(pose.yaw) || 0) + LIDAR_VISUAL_YAW_OFFSET_RAD;
        const startCell = mapWorldToGrid(map, robotX, robotY);
        if (!startCell) return cells;

        for (let index = 0; index < scan.ranges.length; index += 1) {
            const rawDistance = Number(scan.ranges[index]);
            const hasHit = Number.isFinite(rawDistance)
                && rawDistance >= rangeMin
                && rawDistance <= rangeMax;
            const distance = hasHit ? rawDistance : rangeMax;
            if (!Number.isFinite(distance) || distance <= 0) continue;

            const angle = sensorYaw + angleMin + angleIncrement * index;
            const endX = robotX + Math.cos(angle) * distance;
            const endY = robotY + Math.sin(angle) * distance;
            const endCell = mapWorldToGrid(map, endX, endY);
            raytraceComposite(
                cells,
                width,
                height,
                startCell,
                endCell,
                hasHit,
            );
        }

        return cells;
    }

    function renderCurrentMappingComposite() {
        if (
            !isMappingMode()
            || !currentMap
            || !currentBaseMapCells
            || !mappingOverlayScan
            || !validPose(mappingOverlayPose)
        ) return;

        const cells = composeMappingCells(
            currentMap,
            currentBaseMapCells,
            mappingOverlayScan,
            mappingOverlayPose,
        );
        if (!cells) return;

        renderMapCells(
            currentMap,
            cells,
            `dynamic:${currentBaseMapKey || ''}:${mappingOverlayKey || ''}`,
        );
    }

    function rebuildMap(map, revision) {
        const nextBaseKey = mapKey(map, revision);
        if (nextBaseKey && nextBaseKey === currentBaseMapKey) return;

        currentBaseMapKey = nextBaseKey;
        currentMapRevision = revision ?? null;
        currentMap = map || null;

        if (!map) {
            currentBaseMapCells = null;
            mappingOverlayScan = null;
            mappingOverlayPose = null;
            mappingOverlayKey = null;
            renderMapCells(null, null, null);
            return;
        }

        const width = Number(map.width) || 0;
        const height = Number(map.height) || 0;
        const resolution = Number(map.resolution) || 0;
        if (width <= 0 || height <= 0 || resolution <= 0) return;

        currentBaseMapCells = decodeRleMap(map.data, width * height);

        // Base map updates remain immediate. During Mapping, re-apply the most
        // recent 1-second dynamic sample so already mapped content stays intact.
        if (
            isMappingMode()
            && mappingOverlayScan
            && validPose(mappingOverlayPose)
        ) {
            renderCurrentMappingComposite();
        } else {
            renderMapCells(
                currentMap,
                currentBaseMapCells,
                `base:${currentBaseMapKey || ''}`,
            );
        }
    }

    function refreshMappingDynamicMap() {
        if (!isMappingMode()) {
            mappingOverlayScan = null;
            mappingOverlayPose = null;
            mappingOverlayKey = null;
            if (currentMap && currentBaseMapCells) {
                renderMapCells(
                    currentMap,
                    currentBaseMapCells,
                    `base:${currentBaseMapKey || ''}`,
                );
            }
            return;
        }

        const scan = currentVisualizationState?.scan || null;
        const pose = currentVisualizationState?.pose || null;
        if (
            !scan
            || !Array.isArray(scan.ranges)
            || !scan.ranges.length
            || !validPose(pose)
        ) return;

        const nextKey = [
            scanKey(scan) || '',
            Number(pose.x || 0).toFixed(3),
            Number(pose.y || 0).toFixed(3),
            Number(pose.yaw || 0).toFixed(3),
        ].join(':');

        mappingOverlayScan = scan;
        mappingOverlayPose = pose;
        mappingOverlayKey = nextKey;
        renderCurrentMappingComposite();
    }

    window.setInterval(
        refreshMappingDynamicMap,
        MAPPING_DYNAMIC_MAP_REFRESH_MS,
    );

    function clamp(value, min, max) {
        return Math.min(max, Math.max(min, value));
    }

    function loadGlb(url) {
        const loader = new GLTFLoader();
        return new Promise((resolve, reject) => {
            loader.load(url, gltf => resolve(gltf.scene), undefined, reject);
        });
    }

    function applyNeutralMaterial(object, color) {
        const material = new THREE.MeshStandardMaterial({
            color,
            roughness: 0.78,
            metalness: 0.04,
        });
        object.traverse(child => {
            if (!child.isMesh) return;
            disposeMaterial(child.material);
            child.material = material;
            child.castShadow = false;
            child.receiveShadow = false;
        });
    }

    function placePartAtBottom(object, bottomZ) {
        object.updateMatrixWorld(true);
        const box = new THREE.Box3().setFromObject(object);
        const center = box.getCenter(new THREE.Vector3());
        object.position.x -= center.x;
        object.position.y -= center.y;
        object.position.z += bottomZ - box.min.z;
        object.updateMatrixWorld(true);
        return new THREE.Box3().setFromObject(object);
    }

    function createWheel(radius, width, sideSign) {
        const wheel = new THREE.Group();
        wheel.name = sideSign > 0 ? 'left-wheel' : 'right-wheel';

        const tire = new THREE.Mesh(
            new THREE.CylinderGeometry(radius, radius, width, 20),
            new THREE.MeshStandardMaterial({
                color: COLORS.wheel,
                roughness: 0.92,
                metalness: 0.02,
            }),
        );
        wheel.add(tire);

        // A small outer-face spoke makes actual wheel roll visible. The wheel group
        // still rolls around local +Y, matching the RC car's left/right axle.
        const spoke = new THREE.Mesh(
            new THREE.BoxGeometry(
                radius * 1.18,
                Math.max(0.002, width * 0.07),
                Math.max(0.002, radius * 0.10),
            ),
            new THREE.MeshStandardMaterial({
                color: COLORS.robotFront,
                roughness: 0.8,
                metalness: 0.02,
            }),
        );
        spoke.position.y = sideSign * width * 0.52;
        wheel.add(spoke);

        return wheel;
    }

    function createCameraPlaneGeometry(width, height) {
        const halfWidth = width / 2;
        const halfHeight = height / 2;
        const x = cameraMountLocal.x + CAMERA_VIEW_DISTANCE_M;
        const z = cameraMountLocal.z;
        const vertices = new Float32Array([
            x, -halfWidth, z - halfHeight,
            x, halfWidth, z - halfHeight,
            x, halfWidth, z + halfHeight,
            x, -halfWidth, z + halfHeight,
        ]);
        // ROS base_link uses +Y as vehicle-left. The camera plane vertices are
        // ordered from -Y (right) to +Y (left), so U must run in the opposite
        // direction to keep the 3D camera view's left/right physically correct.
        // This mirrors only the WebGL camera plane; the main camera stream is unchanged.
        const uvs = new Float32Array([
            1, 0,
            0, 0,
            0, 1,
            1, 1,
        ]);
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute('position', new THREE.BufferAttribute(vertices, 3));
        geometry.setAttribute('uv', new THREE.BufferAttribute(uvs, 2));
        geometry.setIndex([0, 1, 2, 0, 2, 3]);
        geometry.computeVertexNormals();
        return geometry;
    }

    function createCameraOutlineGeometry(width, height) {
        const halfWidth = width / 2;
        const halfHeight = height / 2;
        const x = cameraMountLocal.x + CAMERA_VIEW_DISTANCE_M + 0.001;
        const z = cameraMountLocal.z;
        return new THREE.BufferGeometry().setFromPoints([
            new THREE.Vector3(x, -halfWidth, z - halfHeight),
            new THREE.Vector3(x, halfWidth, z - halfHeight),
            new THREE.Vector3(x, halfWidth, z + halfHeight),
            new THREE.Vector3(x, -halfWidth, z + halfHeight),
        ]);
    }

    function createFrustumGeometry(width, height) {
        const halfWidth = width / 2;
        const halfHeight = height / 2;
        const origin = cameraMountLocal;
        const x = cameraMountLocal.x + CAMERA_VIEW_DISTANCE_M;
        const z = cameraMountLocal.z;
        const corners = [
            new THREE.Vector3(x, -halfWidth, z - halfHeight),
            new THREE.Vector3(x, halfWidth, z - halfHeight),
            new THREE.Vector3(x, halfWidth, z + halfHeight),
            new THREE.Vector3(x, -halfWidth, z + halfHeight),
        ];
        const vertices = [];
        for (const corner of corners) {
            vertices.push(origin.x, origin.y, origin.z, corner.x, corner.y, corner.z);
        }
        for (let index = 0; index < corners.length; index += 1) {
            const a = corners[index];
            const b = corners[(index + 1) % corners.length];
            vertices.push(a.x, a.y, a.z, b.x, b.y, b.z);
        }
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute(
            'position',
            new THREE.Float32BufferAttribute(vertices, 3),
        );
        return geometry;
    }

    function ensureCameraTexture() {
        if (cameraTexture) return cameraTexture;
        cameraTextureCanvas = document.createElement('canvas');
        cameraTextureCanvas.width = 512;
        cameraTextureCanvas.height = 384;
        cameraTextureContext = cameraTextureCanvas.getContext('2d', {
            alpha: false,
            desynchronized: true,
        });
        cameraTextureContext.fillStyle = '#101821';
        cameraTextureContext.fillRect(
            0,
            0,
            cameraTextureCanvas.width,
            cameraTextureCanvas.height,
        );
        cameraTexture = new THREE.CanvasTexture(cameraTextureCanvas);
        cameraTexture.colorSpace = THREE.SRGBColorSpace;
        cameraTexture.minFilter = THREE.LinearFilter;
        cameraTexture.magFilter = THREE.LinearFilter;
        cameraTexture.generateMipmaps = false;
        return cameraTexture;
    }

    function rebuildCameraGeometry(nextAspect = cameraAspect) {
        const aspect = Number.isFinite(Number(nextAspect)) && Number(nextAspect) > 0
            ? Number(nextAspect)
            : 4 / 3;
        cameraAspect = aspect;
        const viewHeight = CAMERA_VIEW_HEIGHT_M;
        const viewWidth = viewHeight * cameraAspect;

        clearGroup(cameraFrustumPoseGroup);
        cameraFrustumLines = new THREE.LineSegments(
            createFrustumGeometry(viewWidth, viewHeight),
            new THREE.LineBasicMaterial({
                color: COLORS.cameraFrustum,
                transparent: true,
                opacity: 0.68,
                depthTest: false,
                depthWrite: false,
            }),
        );
        cameraFrustumLines.renderOrder = 40;
        cameraFrustumPoseGroup.add(cameraFrustumLines);

        if (cameraViewPlane?.material) {
            cameraViewPlane.material.map = null;
        }
        clearGroup(cameraViewPoseGroup);
        const texture = ensureCameraTexture();
        cameraViewPlane = new THREE.Mesh(
            createCameraPlaneGeometry(viewWidth, viewHeight),
            new THREE.MeshBasicMaterial({
                map: texture,
                side: THREE.DoubleSide,
                toneMapped: false,
                depthTest: false,
                depthWrite: false,
            }),
        );
        cameraViewPlane.frustumCulled = false;
        cameraViewPlane.renderOrder = 50;
        cameraViewPoseGroup.add(cameraViewPlane);

        cameraViewBorder = new THREE.LineLoop(
            createCameraOutlineGeometry(viewWidth, viewHeight),
            new THREE.LineBasicMaterial({
                color: COLORS.cameraViewBorder,
                transparent: true,
                opacity: 0.78,
                depthTest: false,
                depthWrite: false,
            }),
        );
        cameraViewBorder.frustumCulled = false;
        cameraViewBorder.renderOrder = 51;
        cameraViewPoseGroup.add(cameraViewBorder);
        cameraVisualReady = true;
    }

    function updateCameraTexture(now) {
        if (
            !cameraVisualReady
            || !cameraImage
            || !cameraTexture
            || !cameraTextureContext
            || !layerGroups['camera-view'].visible
            || !cameraViewPoseGroup.visible
        ) return;

        if (now - lastCameraTextureAt < 1000 / CAMERA_TEXTURE_FPS) return;
        if (!cameraImage.complete || !cameraImage.naturalWidth || !cameraImage.naturalHeight) return;

        const nextAspect = cameraImage.naturalWidth / cameraImage.naturalHeight;
        if (Math.abs(nextAspect - cameraAspect) > 0.002) {
            rebuildCameraGeometry(nextAspect);
        }

        const targetWidth = Math.max(
            1,
            Math.min(CAMERA_TEXTURE_MAX_WIDTH, cameraImage.naturalWidth),
        );
        const targetHeight = Math.max(1, Math.round(targetWidth / nextAspect));
        if (
            cameraTextureCanvas.width !== targetWidth
            || cameraTextureCanvas.height !== targetHeight
        ) {
            cameraTextureCanvas.width = targetWidth;
            cameraTextureCanvas.height = targetHeight;
        }

        try {
            cameraTextureContext.drawImage(
                cameraImage,
                0,
                0,
                cameraTextureCanvas.width,
                cameraTextureCanvas.height,
            );
            cameraTexture.needsUpdate = true;
            lastCameraTextureAt = now;
        } catch (error) {
            // The existing dashboard stream remains authoritative if a frame
            // is temporarily unavailable to the WebGL texture.
        }
    }

    function addCameraBody(model, chassisLength, chassisWidth, topZ) {
        scanVisualHeightM = topZ + ORIGINAL_CAMERA_HEIGHT_OFFSET_M;
        cameraMountLocal.set(
            chassisLength * CAMERA_MOUNT_FORWARD_RATIO,
            0,
            LIDAR_HEIGHT_M,
        );

        const bodyLength = clamp(chassisLength * 0.08, 0.032, 0.055);
        const bodyWidth = clamp(chassisWidth * 0.24, 0.04, 0.075);
        const bodyHeight = clamp(bodyWidth * 0.68, 0.028, 0.052);
        const body = new THREE.Mesh(
            new THREE.BoxGeometry(bodyLength, bodyWidth, bodyHeight),
            new THREE.MeshStandardMaterial({
                color: COLORS.cameraBody,
                roughness: 0.82,
                metalness: 0.02,
            }),
        );
        body.position.copy(cameraMountLocal);
        model.add(body);

        const lens = new THREE.Mesh(
            new THREE.CylinderGeometry(
                Math.min(bodyWidth, bodyHeight) * 0.22,
                Math.min(bodyWidth, bodyHeight) * 0.22,
                bodyLength * 0.3,
                18,
            ),
            new THREE.MeshStandardMaterial({
                color: COLORS.cameraLens,
                roughness: 0.55,
                metalness: 0.08,
            }),
        );
        lens.rotation.z = Math.PI / 2;
        lens.position.set(
            cameraMountLocal.x + bodyLength * 0.56,
            cameraMountLocal.y,
            cameraMountLocal.z,
        );
        model.add(lens);

        rebuildCameraGeometry(cameraAspect);
        if (currentVisualizationState?.scan) {
            currentScanKey = null;
            rebuildScan(currentVisualizationState.scan);
        }
    }

    async function buildRobotModel() {
        try {
            const [upper, lower] = await Promise.all([
                loadGlb('/static/assets/robot_upper_chassis.glb'),
                loadGlb('/static/assets/robot_lower_chassis.glb'),
            ]);

            applyNeutralMaterial(lower, COLORS.lowerChassis);
            applyNeutralMaterial(upper, COLORS.upperChassis);

            const initialLowerBox = new THREE.Box3().setFromObject(lower);
            const initialLowerSize = initialLowerBox.getSize(new THREE.Vector3());
            const longAxisIsY = initialLowerSize.y > initialLowerSize.x;
            const chassisLength = Math.max(initialLowerSize.x, initialLowerSize.y);
            const chassisWidth = Math.min(initialLowerSize.x, initialLowerSize.y);
            const wheelRadius = clamp(chassisWidth * 0.22, 0.028, 0.05);
            const wheelWidth = clamp(chassisWidth * 0.13, 0.018, 0.038);

            const model = new THREE.Group();
            model.name = 'robot-visual';

            const cad = new THREE.Group();
            cad.name = 'actual-chassis';
            // The physical robot uses the CAD rear as its actual front.
            // Keep +X as the real driving/camera direction and rotate only the CAD shell.
            cad.rotation.z = (longAxisIsY ? -Math.PI / 2 : 0) + Math.PI;

            const lowerBox = placePartAtBottom(lower, wheelRadius * 0.72);
            const upperBox = placePartAtBottom(upper, lowerBox.max.z + 0.012);
            cad.add(lower, upper);
            model.add(cad);

            const wheelX = chassisLength * 0.35;
            const wheelY = chassisWidth * 0.5 + wheelWidth * 0.18;
            wheelVisuals.left.length = 0;
            wheelVisuals.right.length = 0;
            wheelVisualRadiusM = wheelRadius;
            wheelVisualTrackM = wheelY * 2;
            wheelRollRadians.left = 0;
            wheelRollRadians.right = 0;

            for (const x of [-wheelX, wheelX]) {
                for (const y of [-wheelY, wheelY]) {
                    const side = y > 0 ? 'left' : 'right';
                    const wheel = createWheel(
                        wheelRadius,
                        wheelWidth,
                        y > 0 ? 1 : -1,
                    );
                    wheel.position.set(x, y, wheelRadius);
                    wheelVisuals[side].push(wheel);
                    model.add(wheel);
                }
            }

            const frontMarker = new THREE.Mesh(
                new THREE.BoxGeometry(
                    Math.max(0.018, chassisLength * 0.08),
                    Math.max(0.035, chassisWidth * 0.32),
                    0.012,
                ),
                new THREE.MeshStandardMaterial({
                    color: COLORS.robotFront,
                    roughness: 0.7,
                    metalness: 0.02,
                }),
            );
            frontMarker.position.set(
                chassisLength * 0.46,
                0,
                Math.max(upperBox.max.z, wheelRadius * 2) + 0.01,
            );
            model.add(frontMarker);

            addCameraBody(
                model,
                chassisLength,
                chassisWidth,
                Math.max(upperBox.max.z, wheelRadius * 2),
            );

            obstacleHeightM = clamp(wheelRadius * 2.25, 0.07, 0.12);

            clearGroup(robotPoseGroup);
            robotPoseGroup.add(model);
            robotVisual = model;
            robotModelReady = true;

            clearGroup(tfPoseGroup);
            baseAxes = new THREE.AxesHelper(clamp(chassisLength * 0.55, 0.12, 0.3));
            baseAxes.position.z = 0.01;
            tfPoseGroup.add(baseAxes);

            if (targetPose) {
                applyRenderedPose(renderedPose || targetPose);
            }

            if (currentMap) {
                currentMapKey = null;
                rebuildMap(currentMap, window.dabomNavigationVisualizationState?.mapRevision);
            }
        } catch (error) {
            console.error('GLB chassis load failed:', error);
            robotModelReady = false;
        }
    }

    function poseFromPayload(pose) {
        if (
            !pose
            || !Number.isFinite(Number(pose.x))
            || !Number.isFinite(Number(pose.y))
        ) return null;
        return {
            x: Number(pose.x),
            y: Number(pose.y),
            yaw: Number(pose.yaw) || 0,
            z: 0.006,
        };
    }

    function setPoseVisibility(visible) {
        robotPoseGroup.visible = Boolean(visible && robotModelReady);
        cameraFrustumPoseGroup.visible = Boolean(visible && cameraVisualReady);
        cameraViewPoseGroup.visible = Boolean(visible && cameraVisualReady);
        scanRoot.visible = Boolean(visible && scanRayGeometry);
        pointsRoot.visible = Boolean(visible && scanPointGeometry);
    }

    function applyTfPose(pose) {
        const tfPose = poseFromPayload(pose);
        if (!tfPose) {
            tfPoseGroup.visible = false;
            return;
        }
        tfPoseGroup.position.set(tfPose.x, tfPose.y, tfPose.z);
        tfPoseGroup.rotation.z = tfPose.yaw;
        tfPoseGroup.visible = true;
    }

    function applyRenderedPose(pose) {
        if (!pose) {
            setPoseVisibility(false);
            followTarget = null;
            return;
        }

        const poseGroups = [
            robotPoseGroup,
            cameraFrustumPoseGroup,
            cameraViewPoseGroup,
        ];
        for (const group of poseGroups) {
            group.position.set(pose.x, pose.y, pose.z);
            group.rotation.z = pose.yaw;
        }

        // Rotate both LiDAR layers in place around the current robot/LiDAR origin.
        // This fixes the physical mount being reversed without altering ROS/map data.
        for (const group of [scanRoot, pointsRoot]) {
            group.position.set(pose.x, pose.y, pose.z);
            group.rotation.z = pose.yaw + LIDAR_VISUAL_YAW_OFFSET_RAD;
        }
        setPoseVisibility(true);

        followTarget = new THREE.Vector3(pose.x, pose.y, pose.z);
    }

    function normalizedYawDelta(fromYaw, toYaw) {
        return Math.atan2(
            Math.sin(toYaw - fromYaw),
            Math.cos(toYaw - fromYaw),
        );
    }

    function advanceWheelVisuals(previousPose, nextPose) {
        if (
            !previousPose
            || !nextPose
            || wheelVisualRadiusM <= 0
            || wheelVisualTrackM <= 0
        ) return;

        const dx = nextPose.x - previousPose.x;
        const dy = nextPose.y - previousPose.y;
        const yawDelta = normalizedYawDelta(
            previousPose.yaw,
            nextPose.yaw,
        );

        // Project visual displacement onto the differential-drive forward axis.
        // Lateral localization corrections should not make the tires "roll sideways".
        const midpointYaw = previousPose.yaw + yawDelta * 0.5;
        const centerDistance = (
            dx * Math.cos(midpointYaw)
            + dy * Math.sin(midpointYaw)
        );

        const leftDistance = (
            centerDistance
            - yawDelta * wheelVisualTrackM * 0.5
        );
        const rightDistance = (
            centerDistance
            + yawDelta * wheelVisualTrackM * 0.5
        );

        // With +X as vehicle forward and the wheel axle along +Y, positive
        // rotation around local Y is forward wheel roll.
        wheelRollRadians.left += leftDistance / wheelVisualRadiusM;
        wheelRollRadians.right += rightDistance / wheelVisualRadiusM;

        for (const wheel of wheelVisuals.left) {
            wheel.rotation.y = wheelRollRadians.left;
        }
        for (const wheel of wheelVisuals.right) {
            wheel.rotation.y = wheelRollRadians.right;
        }
    }

    function updateRobotPose(pose) {
        const next = poseFromPayload(pose);
        targetPose = next;
        if (!next) {
            renderedPose = null;
            lastAnimationAt = 0;
            applyRenderedPose(null);
            return;
        }

        if (!renderedPose) {
            renderedPose = { ...next };
            applyRenderedPose(renderedPose);
        }
    }

    function interpolateRobotPose(now) {
        if (!targetPose || !renderedPose) return;
        const deltaSec = lastAnimationAt > 0
            ? Math.min(0.1, Math.max(0, (now - lastAnimationAt) / 1000))
            : 0;
        lastAnimationAt = now;
        if (deltaSec <= 0) return;

        const errorX = targetPose.x - renderedPose.x;
        const errorY = targetPose.y - renderedPose.y;
        const distance = Math.hypot(errorX, errorY);
        const yawError = normalizedYawDelta(
            renderedPose.yaw,
            targetPose.yaw,
        );

        // A localization jump is a correction, not physical wheel travel.
        if (distance > MAX_VISUAL_TELEPORT_M) {
            renderedPose = { ...targetPose };
            applyRenderedPose(renderedPose);
            return;
        }

        const previousPose = { ...renderedPose };
        const forwardAlpha = 1 - Math.exp(
            -POSE_FORWARD_RESPONSE_PER_SEC * deltaSec
        );
        const lateralAlpha = 1 - Math.exp(
            -POSE_LATERAL_RESPONSE_PER_SEC * deltaSec
        );
        const yawAlpha = 1 - Math.exp(
            -POSE_YAW_RESPONSE_PER_SEC * deltaSec
        );

        // For a differential-drive RC car, the displacement chord of a turn is
        // aligned with the midpoint heading. Give that direction the normal
        // response and absorb lateral AMCL/map corrections more gently.
        const midpointYaw = renderedPose.yaw + yawError * 0.5;
        const forwardX = Math.cos(midpointYaw);
        const forwardY = Math.sin(midpointYaw);
        const lateralX = -forwardY;
        const lateralY = forwardX;
        const forwardError = errorX * forwardX + errorY * forwardY;
        const lateralError = errorX * lateralX + errorY * lateralY;

        renderedPose.x += (
            forwardX * forwardError * forwardAlpha
            + lateralX * lateralError * lateralAlpha
        );
        renderedPose.y += (
            forwardY * forwardError * forwardAlpha
            + lateralY * lateralError * lateralAlpha
        );
        renderedPose.z += (
            targetPose.z - renderedPose.z
        ) * forwardAlpha;
        renderedPose.yaw += yawError * yawAlpha;

        // Wheel roll follows the exact pose shown on screen. Turning in place
        // naturally drives left/right wheel visuals in opposite directions.
        advanceWheelVisuals(previousPose, renderedPose);
        applyRenderedPose(renderedPose);
    }

    function validPose(pose) {
        return Boolean(
            pose
            && Number.isFinite(Number(pose.x))
            && Number.isFinite(Number(pose.y))
        );
    }

    function scanKey(scan) {
        if (!scan || !Array.isArray(scan.ranges)) return null;
        return [
            scan.timestamp || '',
            scan.received_at || '',
            scan.ranges.length,
            scan.ranges[0] ?? '',
            scan.ranges[scan.ranges.length - 1] ?? '',
        ].join(':');
    }

    function nextPowerOfTwo(value) {
        let result = 1;
        while (result < value) result *= 2;
        return result;
    }

    function ensureScanCapacity(required) {
        if (required <= scanCapacity && scanRayGeometry && scanPointGeometry) return;

        scanCapacity = nextPowerOfTwo(Math.max(1, required));
        scanRayPositions = new Float32Array(scanCapacity * 2 * 3);
        scanPointPositions = new Float32Array(scanCapacity * 3);

        clearGroup(scanRoot);
        clearGroup(pointsRoot);

        scanRayGeometry = new THREE.BufferGeometry();
        const rayAttribute = new THREE.BufferAttribute(scanRayPositions, 3);
        rayAttribute.setUsage(THREE.DynamicDrawUsage);
        scanRayGeometry.setAttribute('position', rayAttribute);
        scanRayGeometry.setDrawRange(0, 0);

        const rays = new THREE.LineSegments(
            scanRayGeometry,
            new THREE.LineBasicMaterial({
                color: COLORS.scanRay,
                transparent: true,
                opacity: 0.34,
                depthWrite: false,
            }),
        );
        rays.frustumCulled = false;
        scanRoot.add(rays);

        scanPointGeometry = new THREE.BufferGeometry();
        const pointAttribute = new THREE.BufferAttribute(scanPointPositions, 3);
        pointAttribute.setUsage(THREE.DynamicDrawUsage);
        scanPointGeometry.setAttribute('position', pointAttribute);
        scanPointGeometry.setDrawRange(0, 0);

        const points = new THREE.Points(
            scanPointGeometry,
            new THREE.PointsMaterial({
                color: COLORS.scanPoint,
                size: 0.035,
                sizeAttenuation: true,
                transparent: true,
                opacity: 0.95,
                depthWrite: false,
            }),
        );
        points.frustumCulled = false;
        pointsRoot.add(points);
    }

    // Same behavior as RViz2 LaserScan with Decay Time = 0:
    // every incoming scan replaces the previous frame. A transient obstacle
    // disappears from the live LiDAR layer on the next valid scan.
    function rebuildScan(scan) {
        const nextKey = scanKey(scan);
        if (nextKey === currentScanKey) return;
        currentScanKey = nextKey;

        if (!scan || !Array.isArray(scan.ranges) || !scan.ranges.length) {
            if (scanRayGeometry) scanRayGeometry.setDrawRange(0, 0);
            if (scanPointGeometry) scanPointGeometry.setDrawRange(0, 0);
            scanRoot.visible = false;
            pointsRoot.visible = false;
            return;
        }

        ensureScanCapacity(scan.ranges.length);

        const angleMin = Number(scan.angle_min) || 0;
        const angleIncrement = Number(scan.angle_increment) || 0;
        const rangeMin = Math.max(0, Number(scan.range_min) || 0);
        const rangeMax = Number.isFinite(Number(scan.range_max))
            ? Number(scan.range_max)
            : 12;

        let validCount = 0;
        for (let index = 0; index < scan.ranges.length; index += 1) {
            const distance = Number(scan.ranges[index]);
            if (!Number.isFinite(distance) || distance < rangeMin || distance > rangeMax) continue;

            const angle = angleMin + angleIncrement * index;
            const x = Math.cos(angle) * distance;
            const y = Math.sin(angle) * distance;

            const rayOffset = validCount * 6;
            scanRayPositions[rayOffset] = 0;
            scanRayPositions[rayOffset + 1] = 0;
            scanRayPositions[rayOffset + 2] = scanVisualHeightM;
            scanRayPositions[rayOffset + 3] = x;
            scanRayPositions[rayOffset + 4] = y;
            scanRayPositions[rayOffset + 5] = scanVisualHeightM;

            const pointOffset = validCount * 3;
            scanPointPositions[pointOffset] = x;
            scanPointPositions[pointOffset + 1] = y;
            scanPointPositions[pointOffset + 2] = scanVisualHeightM;
            validCount += 1;
        }

        const rayAttribute = scanRayGeometry.attributes.position;
        const pointAttribute = scanPointGeometry.attributes.position;
        rayAttribute.clearUpdateRanges?.();
        pointAttribute.clearUpdateRanges?.();
        rayAttribute.addUpdateRange?.(0, validCount * 6);
        pointAttribute.addUpdateRange?.(0, validCount * 3);
        rayAttribute.needsUpdate = true;
        pointAttribute.needsUpdate = true;
        scanRayGeometry.setDrawRange(0, validCount * 2);
        scanPointGeometry.setDrawRange(0, validCount);
        scanRoot.visible = Boolean(validCount && targetPose);
        pointsRoot.visible = Boolean(validCount && targetPose);
    }

    function pathKey(path) {
        if (!Array.isArray(path) || !path.length) return 'empty';
        let hash = 2166136261;
        for (const point of path) {
            const x = Math.round((Number(point?.x) || 0) * 1000);
            const y = Math.round((Number(point?.y) || 0) * 1000);
            hash ^= x;
            hash = Math.imul(hash, 16777619);
            hash ^= y;
            hash = Math.imul(hash, 16777619);
        }
        return `${path.length}:${hash >>> 0}`;
    }

    function rebuildPath(path) {
        const nextKey = pathKey(path);
        if (nextKey === currentPathKey) return;
        currentPathKey = nextKey;

        clearGroup(pathRoot);
        if (!Array.isArray(path) || path.length < 2) return;

        const positions = [];
        for (const point of path) {
            const x = Number(point?.x);
            const y = Number(point?.y);
            if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
            positions.push(x, y, 0.035);
        }
        if (positions.length < 6) return;

        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute(
            'position',
            new THREE.Float32BufferAttribute(positions, 3),
        );
        const line = new THREE.Line(
            geometry,
            new THREE.LineBasicMaterial({
                color: COLORS.globalPath,
                transparent: true,
                opacity: 0.92,
            }),
        );
        line.frustumCulled = false;
        pathRoot.add(line);
    }

    function createGoalMarker(goal, color) {
        const x = Number(goal?.x);
        const y = Number(goal?.y);
        if (!Number.isFinite(x) || !Number.isFinite(y)) return null;

        const yaw = Number(goal?.yaw) || 0;
        const marker = new THREE.Group();
        marker.position.set(x, y, 0.045);
        marker.rotation.z = yaw;

        const ring = new THREE.Mesh(
            new THREE.RingGeometry(0.07, 0.105, 32),
            new THREE.MeshBasicMaterial({
                color,
                side: THREE.DoubleSide,
                transparent: true,
                opacity: 0.95,
                depthWrite: false,
            }),
        );
        marker.add(ring);

        const direction = new THREE.ArrowHelper(
            new THREE.Vector3(1, 0, 0),
            new THREE.Vector3(0, 0, 0.012),
            0.30,
            color,
            0.085,
            0.05,
        );
        marker.add(direction);
        return marker;
    }

    function goalKey(goal) {
        if (!goal) return '';
        return [
            Number(goal.x).toFixed(4),
            Number(goal.y).toFixed(4),
            Number(goal.yaw || 0).toFixed(4),
        ].join(':');
    }

    function rebuildGoals() {
        const nextKey = `${goalKey(currentControlState?.active_goal)}|${goalKey(currentDraftGoal)}`;
        if (nextKey === currentGoalKey) return;
        currentGoalKey = nextKey;

        clearGroup(goalRoot);
        const active = createGoalMarker(currentControlState?.active_goal, COLORS.goal);
        if (active) goalRoot.add(active);
        const draft = createGoalMarker(currentDraftGoal, COLORS.draftGoal);
        if (draft) goalRoot.add(draft);
    }

    function ensureTrajectoryBuffer() {
        if (trajectoryGeometry) return;

        trajectoryPositions = new Float32Array(MAX_TRAJECTORY_POINTS * 3);
        trajectoryGeometry = new THREE.BufferGeometry();
        const attribute = new THREE.BufferAttribute(trajectoryPositions, 3);
        attribute.setUsage(THREE.DynamicDrawUsage);
        trajectoryGeometry.setAttribute('position', attribute);
        trajectoryGeometry.setDrawRange(0, 0);

        const line = new THREE.Line(
            trajectoryGeometry,
            new THREE.LineBasicMaterial({
                color: COLORS.trajectory,
                transparent: true,
                opacity: 0.9,
            }),
        );
        line.frustumCulled = false;

        const points = new THREE.Points(
            trajectoryGeometry,
            new THREE.PointsMaterial({
                color: COLORS.trajectory,
                size: 0.045,
                sizeAttenuation: true,
                transparent: true,
                opacity: 0.95,
                depthWrite: false,
            }),
        );
        points.frustumCulled = false;
        trajectoryRoot.add(line, points);
    }

    function writeTrajectoryPoint(index, point) {
        const offset = index * 3;
        trajectoryPositions[offset] = point.x;
        trajectoryPositions[offset + 1] = point.y;
        trajectoryPositions[offset + 2] = 0.045;
    }

    function markTrajectoryRangeUpdated(offset, count) {
        const attribute = trajectoryGeometry.attributes.position;
        attribute.clearUpdateRanges?.();
        attribute.addUpdateRange?.(offset, count);
        attribute.needsUpdate = true;
    }

    function renderTrajectory() {
        ensureTrajectoryBuffer();
        const count = Math.min(trajectorySamples.length, MAX_TRAJECTORY_POINTS);
        for (let index = 0; index < count; index += 1) {
            writeTrajectoryPoint(index, trajectorySamples[index]);
        }
        trajectoryGeometry.setDrawRange(0, count);
        if (count) markTrajectoryRangeUpdated(0, count * 3);
    }

    function appendTrajectoryPoint(point) {
        ensureTrajectoryBuffer();
        const index = trajectorySamples.length - 1;
        if (index < 0 || index >= MAX_TRAJECTORY_POINTS) return;
        writeTrajectoryPoint(index, point);
        trajectoryGeometry.setDrawRange(0, trajectorySamples.length);
        markTrajectoryRangeUpdated(index * 3, 3);
    }

    function resetTrajectory(seedPose = null, active = false) {
        trajectorySamples = [];
        lastTrajectorySampleAt = 0;
        trajectorySessionActive = active;
        if (active && validPose(seedPose)) {
            trajectorySamples.push({
                x: Number(seedPose.x),
                y: Number(seedPose.y),
            });
            lastTrajectorySampleAt = performance.now();
        }
        renderTrajectory();
    }

    function sampleTrajectory(pose) {
        if (!trajectorySessionActive || !validPose(pose)) return;
        const next = { x: Number(pose.x), y: Number(pose.y) };
        const last = trajectorySamples[trajectorySamples.length - 1];
        if (!last) {
            trajectorySamples.push(next);
            lastTrajectorySampleAt = performance.now();
            renderTrajectory();
            return;
        }

        const distance = Math.hypot(next.x - last.x, next.y - last.y);
        const now = performance.now();
        const fallbackReached = (
            now - lastTrajectorySampleAt >= TRAJECTORY_FALLBACK_MS
            && distance >= TRAJECTORY_FALLBACK_DISTANCE_M
        );
        if (distance < TRAJECTORY_DISTANCE_M && !fallbackReached) return;

        trajectorySamples.push(next);
        if (trajectorySamples.length > MAX_TRAJECTORY_POINTS) {
            trajectorySamples.splice(
                0,
                trajectorySamples.length - MAX_TRAJECTORY_POINTS,
            );
            renderTrajectory();
        } else {
            appendTrajectoryPoint(next);
        }
        lastTrajectorySampleAt = now;
    }

    function goalInteractionAllowed(control = currentControlState) {
        return Boolean(
            viewerExpanded
            && String(control?.navigation_mode || '').toUpperCase() === 'DRIVING'
            && control?.localization_ready
            && control?.nav2_ready
            && !control?.emergency_stop
            && control?.connected === true
        );
    }

    function syncGoalInteractionAvailability(control = currentControlState) {
        const allowed = goalInteractionAllowed(control);
        const button = root.querySelector('[data-lidar-interaction="set-goal"]');
        if (button) {
            button.disabled = !allowed;
            button.setAttribute('aria-disabled', String(!allowed));
            button.title = allowed
                ? '지도에서 Goal 위치와 방향을 지정합니다.'
                : 'DRIVING 및 Pi·Localization·Nav2 준비 후 사용할 수 있습니다.';
        }
        if (!allowed && interactionMode === 'set-goal') {
            setInteractionMode('view');
        }
    }

    function applyControlState(control) {
        if (!control) return;
        const mode = String(control.navigation_mode || '').toUpperCase();
        const navState = String(control.navigation_state || '').toUpperCase();
        const previousWasMoving = ['NAVIGATING', 'RESUMING'].includes(previousNavigationState);
        const movingNow = ['NAVIGATING', 'RESUMING'].includes(navState);
        const modeRestarted = (
            mode !== previousNavigationMode
            && (mode === 'DRIVING' || mode === 'MAPPING')
        );
        const newDrivingRun = (
            mode === 'DRIVING'
            && navState === 'NAVIGATING'
            && !previousWasMoving
        );

        currentControlState = control;
        syncGoalInteractionAvailability(control);
        rebuildPath(control.planned_path || []);
        rebuildGoals();

        if (mode === 'MAPPING') {
            if (modeRestarted || trajectorySamples.length) {
                resetTrajectory(null, false);
            }
            trajectoryRoot.visible = false;
            if (currentVisualizationState) {
                applyVisualizationState(currentVisualizationState);
            } else {
                updateRobotPose(MAPPING_PREVIEW_POSE);
            }
        } else if (mode === 'DRIVING') {
            trajectoryRoot.visible = true;
            if (modeRestarted) {
                resetTrajectory(null, false);
            }
            if (newDrivingRun || (movingNow && !trajectorySessionActive)) {
                resetTrajectory(currentVisualizationState?.pose, true);
            }
        } else {
            trajectoryRoot.visible = false;
        }

        previousNavigationMode = mode;
        previousNavigationState = navState;
        scheduleControlOverflowLayout();
    }

    function pointInsideCurrentMap(x, y) {
        if (!currentMap) return false;
        const width = Number(currentMap.width) || 0;
        const height = Number(currentMap.height) || 0;
        const resolution = Number(currentMap.resolution) || 0;
        if (width <= 0 || height <= 0 || resolution <= 0) return false;

        const transform = mapTransform(currentMap);
        const dx = x - transform.x;
        const dy = y - transform.y;
        const cosYaw = Math.cos(transform.yaw);
        const sinYaw = Math.sin(transform.yaw);
        const localX = cosYaw * dx + sinYaw * dy;
        const localY = -sinYaw * dx + cosYaw * dy;
        return (
            localX >= 0
            && localY >= 0
            && localX < width * resolution
            && localY < height * resolution
        );
    }

    function screenToGround(event) {
        if (!currentMap) return null;
        const rect = canvas.getBoundingClientRect();
        if (!rect.width || !rect.height) return null;

        const pointer = new THREE.Vector2(
            ((event.clientX - rect.left) / rect.width) * 2 - 1,
            -((event.clientY - rect.top) / rect.height) * 2 + 1,
        );
        raycaster.setFromCamera(pointer, camera);

        const z = mapTransform(currentMap).z;
        groundPlane.set(new THREE.Vector3(0, 0, 1), -z);
        const hit = new THREE.Vector3();
        if (!raycaster.ray.intersectPlane(groundPlane, hit)) return null;
        if (!pointInsideCurrentMap(hit.x, hit.y)) return null;
        return { x: hit.x, y: hit.y };
    }

    function setInteractionMode(mode) {
        interactionMode = (
            mode === 'set-goal' && goalInteractionAllowed()
                ? 'set-goal'
                : 'view'
        );
        controls.enabled = viewerExpanded;
        controls.enableRotate = viewerExpanded && interactionMode === 'view';
        controls.enablePan = viewerExpanded && interactionMode === 'view';
        controls.enableZoom = viewerExpanded;
        root.closest('.lidar-viewer-overlay')?.classList.toggle(
            'goal-input-active',
            viewerExpanded && interactionMode === 'set-goal',
        );
        root.querySelectorAll('[data-lidar-interaction]').forEach(button => {
            button.classList.toggle(
                'active',
                button.dataset.lidarInteraction === interactionMode,
            );
        });
        if (interactionMode === 'set-goal') canvas.focus({ preventScroll: true });
    }

    function isMappingMode() {
        return String(
            currentControlState?.navigation_mode || ''
        ).toUpperCase() === 'MAPPING';
    }

    function payloadMatchesMappingSession(payload) {
        if (!payload) return false;
        return String(
            payload.navigation_mode || ''
        ).toLowerCase() === 'mapping';
    }

    function applyVisualizationState(state) {
        if (!state) return;
        currentVisualizationState = state;

        // Mapping transition now clears server-side map/pose state before
        // the new slam_toolbox session starts. Therefore the latest state received
        // after reset is authoritative and must be rendered immediately, just like
        // RViz2 renders the newest /map and TF messages.
        const mappingMode = isMappingMode();
        const liveMap = state.map || null;
        const livePose = state.pose || null;
        // Raw LiDAR is a live robot-relative sensor stream. Do not gate it
        // by the Mapping session tag: the high-rate Pi WebSocket path may update
        // before the ROS map bridge has produced its first tagged Mapping sample.
        // Map/pose still stay session-filtered so stale Driving localization is
        // never reused as Mapping geometry.
        const liveScan = state.scan || null;

        rebuildMap(liveMap, liveMap ? state.mapRevision : null);

        // Before slam_toolbox exposes map->base_link, keep the RC car visible at
        // the mapping origin. As soon as the real TF pose arrives it replaces this
        // preview pose and the existing interpolation takes over.
        const visualizationPose = validPose(livePose)
            ? livePose
            : (mappingMode ? MAPPING_PREVIEW_POSE : null);

        // TF shows only the actual raw map->base_link transform. The mapping
        // preview keeps the RC car visible but must never fabricate a TF frame.
        applyTfPose(livePose);
        updateRobotPose(visualizationPose);
        rebuildScan(liveScan);

        if (
            trajectorySessionActive
            && String(currentControlState?.navigation_mode || '').toUpperCase() === 'DRIVING'
        ) {
            sampleTrajectory(state.pose || null);
        }
    }

    const setActiveViewButton = mode => {
        root.querySelectorAll('[data-lidar-view]').forEach(button => {
            button.classList.toggle('active', button.dataset.lidarView === mode);
        });
    };

    function applyViewMode(mode) {
        viewMode = ['free', 'top', 'follow'].includes(mode) ? mode : 'free';
        setActiveViewButton(viewMode);

        if (viewMode === 'top') {
            if (!viewerExpanded) {
                applyCollapsedTopView();
                return;
            }
            const target = followTarget || controls.target;
            const distance = Math.max(4, camera.position.distanceTo(controls.target));
            controls.target.copy(target);
            camera.position.set(target.x, target.y, target.z + distance);
            camera.up.set(0, 1, 0);
            camera.lookAt(target);
            controls.update();
            return;
        }

        camera.up.set(0, 0, 1);
        if (viewerExpanded && viewMode === 'follow' && followTarget) {
            controls.target.copy(followTarget);
        }
        controls.update();
    }

    function setViewMode(mode) {
        if (!viewerExpanded) return;
        expandedViewMode = ['free', 'top', 'follow'].includes(mode) ? mode : 'free';
        applyViewMode(expandedViewMode);
    }

    function setExpandedState(expanded) {
        const nextExpanded = Boolean(expanded);
        if (viewerExpanded === nextExpanded) {
            if (!viewerExpanded) applyCollapsedTopView();
            return;
        }

        if (!nextExpanded) {
            expandedViewMode = viewMode;
        }
        viewerExpanded = nextExpanded;
        setInteractionMode('view');
        syncGoalInteractionAvailability();

        if (viewerExpanded) {
            applyViewMode(expandedViewMode || 'free');
        } else {
            applyViewMode('top');
        }
        resize();
        scheduleControlOverflowLayout();
    }

    root.querySelectorAll('[data-lidar-view]').forEach(button => {
        button.addEventListener('click', () => setViewMode(button.dataset.lidarView));
    });

    root.querySelectorAll('[data-lidar-interaction]').forEach(button => {
        button.addEventListener('click', () => {
            setInteractionMode(button.dataset.lidarInteraction);
        });
    });

    root.querySelectorAll('[data-lidar-display]').forEach(input => {
        const group = layerGroups[input.dataset.lidarDisplay];
        if (!group) return;
        group.visible = input.checked;
        input.addEventListener('change', () => {
            group.visible = input.checked;
        });
    });

    controls.addEventListener('start', () => {
        if (!viewerExpanded) return;
        if (viewMode === 'top') {
            viewMode = 'free';
            expandedViewMode = 'free';
            setActiveViewButton('free');
            camera.up.set(0, 0, 1);
        }
    });

    function resize() {
        const rect = root.getBoundingClientRect();
        const width = Math.max(1, Math.floor(rect.width));
        const height = Math.max(1, Math.floor(rect.height));
        const renderWidth = Math.max(1, Math.floor(width * renderer.getPixelRatio()));
        const renderHeight = Math.max(1, Math.floor(height * renderer.getPixelRatio()));
        if (canvas.width !== renderWidth || canvas.height !== renderHeight) {
            renderer.setSize(width, height, false);
            camera.aspect = width / height;
            camera.updateProjectionMatrix();
            if (!viewerExpanded) applyCollapsedTopView();
        }
        scheduleControlOverflowLayout();
    }

    function animate(now = performance.now()) {
        interpolateRobotPose(now);

        if (viewerExpanded && viewMode === 'follow' && followTarget) {
            const delta = followTarget.clone().sub(controls.target);
            if (delta.lengthSq() > 1e-10) {
                camera.position.add(delta);
                controls.target.copy(followTarget);
            }
        }

        updateCameraTexture(now);
        controls.update();
        renderer.render(scene, camera);
        requestAnimationFrame(animate);
    }

    const observer = new ResizeObserver(resize);
    observer.observe(root);

    controlStrip?.addEventListener('wheel', event => {
        if (controlStrip.scrollWidth <= controlStrip.clientWidth + 1) return;
        if (Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return;
        event.preventDefault();
        controlStrip.scrollLeft += event.deltaY;
    }, { passive: false });

    function syncControlDrawerState(open) {
        const isOpen = Boolean(open);
        viewerOverlay?.classList.toggle('controls-collapsed', !isOpen);
        if (!isOpen) setControlOverflowOpen(false);
        scheduleControlOverflowLayout();
    }

    document.addEventListener('dabom:drawer-toggle', event => {
        const detail = event.detail || {};
        if (detail.targetId === 'lidar-control-drawer') {
            syncControlDrawerState(detail.open);
        }
    });

    controlOverflowToggle?.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        setControlOverflowOpen(true);
    });

    document.addEventListener('dabom:navigation-visualization-state', event => {
        applyVisualizationState(event.detail);
    });

    document.addEventListener('dabom:navigation-control-state', event => {
        applyControlState(event.detail);
    });

    document.addEventListener('dabom:navigation-goal-draft', event => {
        currentDraftGoal = event.detail || null;
        rebuildGoals();
    });

    window.addEventListener('beforeunload', () => {
        observer.disconnect();
        if (overflowLayoutFrame) cancelAnimationFrame(overflowLayoutFrame);
        controls.dispose();
        clearGroup(mapRoot);
        clearGroup(gridRoot);
        clearGroup(robotPoseGroup);
        clearGroup(tfPoseGroup);
        clearGroup(scanRoot);
        clearGroup(pointsRoot);
        clearGroup(pathRoot);
        clearGroup(trajectoryRoot);
        clearGroup(goalRoot);
        scanRayGeometry = null;
        scanPointGeometry = null;
        trajectoryGeometry = null;
        clearGroup(cameraFrustumPoseGroup);
        clearGroup(cameraViewPoseGroup);
        cameraTexture?.dispose?.();
        renderer.dispose();
    }, { once: true });

    window.dabomLidar3D = {
        THREE,
        renderer,
        scene,
        camera,
        controls,
        world,
        layers: layerGroups,
        setViewMode,
        setExpandedState,
        setInteractionMode,
        interactionMode() {
            return interactionMode;
        },
        screenToGround,
        setFollowTarget(target) {
            followTarget = target ? new THREE.Vector3(target.x, target.y, target.z || 0) : null;
            if (viewMode === 'follow' && followTarget) controls.target.copy(followTarget);
        },
    };

    buildRobotModel();
    applyVisualizationState(window.dabomNavigationVisualizationState);
    applyControlState(window.dabomNavigationControlState);
    window.DabomDashboardComponents?.controls?.drawerToggle?.enhanceAll?.();
    syncControlDrawerState(controlDrawer?.dataset.open !== 'false');
    setInteractionMode('view');
    resize();
    scheduleControlOverflowLayout();
    if (viewerExpanded) {
        applyViewMode(expandedViewMode);
    } else {
        applyViewMode('top');
    }
    animate();
}
