import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

const root = document.getElementById('lidar-3d-viewer');
const canvas = document.getElementById('lidar-3d-canvas');

if (root && canvas) {
    const COLORS = {
        background: 0x0b1420,
        mapFree: [56, 83, 103, 255],
        mapUnknown: [29, 42, 56, 255],
        mapOccupied: [82, 104, 123, 255],
        occupiedTop: 0x8297a9,
        occupiedWall: 0x52687b,
        mapOutline: 0x577184,
        lowerChassis: 0x687a89,
        upperChassis: 0xa5b2bd,
        wheel: 0x14191e,
        robotFront: 0x5b9bd5,
        scanRay: 0x7dd3fc,
        scanPoint: 0x2dd4bf,
        dynamicObstacle: 0xf97316,
        inflation: 0xfb923c,
        globalPath: 0x8b5cf6,
        trajectory: 0xf59e0b,
        goal: 0xfb7185,
        draftGoal: 0xfbbf24,
        cameraBody: 0x303941,
        cameraLens: 0x111827,
        cameraFrustum: 0xf59aa8,
        cameraViewBorder: 0xe2e8f0,
    };

    const DEFAULT_OCCUPIED_HEIGHT_M = 0.24;
    const DYNAMIC_OBSTACLE_HEIGHT_M = 0.18;
    const INFLATION_HEIGHT_M = 0.006;
    const MAP_OCCUPIED_THRESHOLD = 50;
    const LIDAR_HEIGHT_M = 0.12;
    // The physical LiDAR is mounted 180° relative to the 3D viewer's +X heading.
    // Keep map/pose/navigation coordinates unchanged and rotate only scan visuals.
    const LIDAR_VISUAL_YAW_OFFSET_RAD = Math.PI;
    const MAPPING_PREVIEW_POSE = Object.freeze({ x: 0, y: 0, yaw: 0 });
    const MAPPING_PREVIEW_GRID_SIZE_M = 10;
    // Visual pose smoothing follows differential-drive motion: forward motion responds
    // faster than lateral map/localization correction so the chassis does not appear
    // to slide sideways across its fixed wheel direction.
    const POSE_FORWARD_RESPONSE_PER_SEC = 7.0;
    const POSE_LATERAL_RESPONSE_PER_SEC = 2.5;
    const POSE_YAW_RESPONSE_PER_SEC = 9.0;
    const MAX_VISUAL_TELEPORT_M = 1.5;
    const TRAJECTORY_DISTANCE_M = 0.10;
    const TRAJECTORY_FALLBACK_DISTANCE_M = 0.06;
    const TRAJECTORY_FALLBACK_MS = 1000;
    const TRAJECTORY_FILTER_ALPHA = 0.45;
    const TRAJECTORY_MAX_JUMP_M = 0.75;
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
        'obstacles',
        'inflation',
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

    const ambient = new THREE.HemisphereLight(0xd8e5ef, 0x1d2832, 1.10);
    scene.add(ambient);

    const keyLight = new THREE.DirectionalLight(0xf2f7fa, 1.30);
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

    const dynamicObstacleRoot = new THREE.Group();
    dynamicObstacleRoot.name = 'dynamic-obstacles';
    layerGroups.obstacles.add(dynamicObstacleRoot);

    const inflationRoot = new THREE.Group();
    inflationRoot.name = 'inflation-area';
    layerGroups.inflation.add(inflationRoot);

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
    let currentMap = null;
    let currentCostmap = null;
    let currentObstacleCostmapKey = null;
    let currentInflationCostmapKey = null;
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
    let currentLoopDraft = [];
    let previousNavigationMode = null;
    let previousNavigationState = null;
    let trajectorySessionActive = false;
    let trajectorySamples = [];
    let trajectoryFilteredPose = null;
    let lastTrajectorySampleAt = 0;
    let cameraVisualReady = false;
    let cameraMountLocal = new THREE.Vector3(0.12, 0, LIDAR_HEIGHT_M);
    let scanVisualHeightM = 0.16;
    // Default is the legacy physical 180-degree mounting correction.
    // New scan packets carry the ROS laser->base_link mounting transform.
    let scanMountLocal = { x: 0, y: 0, yaw: LIDAR_VISUAL_YAW_OFFSET_RAD };
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

        const width = Number(map?.width) || 0;
        const height = Number(map?.height) || 0;
        const resolution = Number(map?.resolution) || 0;
        const hasMapExtent = width > 0 && height > 0 && resolution > 0;

        const widthM = hasMapExtent ? width * resolution : MAPPING_PREVIEW_GRID_SIZE_M;
        const heightM = hasMapExtent ? height * resolution : MAPPING_PREVIEW_GRID_SIZE_M;
        const size = Math.max(1, Math.ceil(Math.max(widthM, heightM)));
        const divisions = Math.min(100, Math.max(4, size * 2));
        const helper = new THREE.GridHelper(size, divisions, 0x577184, 0x253c4e);
        helper.material.transparent = true;
        helper.material.opacity = viewerExpanded ? 0.18 : 0.10;
        helper.material.depthWrite = false;
        helper.rotation.x = Math.PI / 2;
        helper.position.set(
            hasMapExtent ? widthM / 2 : 0,
            hasMapExtent ? heightM / 2 : 0,
            0.001,
        );
        gridRoot.add(helper);

        const transform = hasMapExtent ? mapTransform(map) : { x: 0, y: 0, z: 0, yaw: 0 };
        gridRoot.position.set(transform.x, transform.y, transform.z);
        gridRoot.rotation.z = transform.yaw;
    }

    function rebuildMap(map, revision) {
        const nextKey = mapKey(map, revision);
        if (nextKey && nextKey === currentMapKey) return;

        clearGroup(mapRoot);
        currentMapKey = nextKey;
        currentMap = map || null;

        if (!map) {
            rebuildGrid(null);
            return;
        }

        const width = Number(map.width) || 0;
        const height = Number(map.height) || 0;
        const resolution = Number(map.resolution) || 0;
        if (width <= 0 || height <= 0 || resolution <= 0) return;

        const cells = decodeRleMap(map.data, width * height);
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
            // Face-local vertex colors give a bright +Z top and darker
            // sides without six material groups / draw calls. BoxGeometry
            // uses four distinct vertices per face: +Z is face index 4.
            // Extrusion height stays a schematic, not a measurement.
            const faceColors = new Float32Array(boxGeometry.attributes.position.count * 3);
            const topColor = new THREE.Color(COLORS.occupiedTop);
            const sideColor = new THREE.Color(COLORS.occupiedWall);
            for (let vertex = 0; vertex < boxGeometry.attributes.position.count; vertex += 1) {
                const color = Math.floor(vertex / 4) === 4 ? topColor : sideColor;
                faceColors[vertex * 3] = color.r;
                faceColors[vertex * 3 + 1] = color.g;
                faceColors[vertex * 3 + 2] = color.b;
            }
            boxGeometry.setAttribute('color', new THREE.BufferAttribute(faceColors, 3));
            const occupied = new THREE.InstancedMesh(
                boxGeometry,
                new THREE.MeshStandardMaterial({
                    color: 0xffffff, vertexColors: true,
                    roughness: 0.87, metalness: 0.01,
                }),
                occupiedCount,
            );
            occupied.name = 'occupied-cells';
            occupied.instanceMatrix.setUsage(THREE.StaticDrawUsage);

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

        // Thin schematic map extent: no collision, costmap, or raycast role.
        // Inherits the exact map origin and yaw from the occupancy-map root.
        const outline = new THREE.LineLoop(
            new THREE.BufferGeometry().setFromPoints([
                new THREE.Vector3(0, 0, 0.012),
                new THREE.Vector3(widthM, 0, 0.012),
                new THREE.Vector3(widthM, heightM, 0.012),
                new THREE.Vector3(0, heightM, 0.012),
            ]),
            new THREE.LineBasicMaterial({
                color: COLORS.mapOutline, transparent: true,
                opacity: 0.85, depthWrite: false, toneMapped: false,
            }),
        );
        outline.name = 'occupancy-extent-outline';
        outline.raycast = () => {};
        mapRoot.add(outline);

        const transform = mapTransform(map);
        mapRoot.position.set(transform.x, transform.y, transform.z);
        mapRoot.rotation.z = transform.yaw;
        rebuildGrid(map);
        if (!viewerExpanded) applyCollapsedTopView();
    }

    function costmapKey(costmap) {
        if (!costmap || typeof costmap !== 'object') return 'empty';
        const origin = costmap.origin || {};
        return [
            costmap.received_at || '',
            Number(costmap.width) || 0,
            Number(costmap.height) || 0,
            Number(costmap.resolution) || 0,
            Number(origin.x) || 0,
            Number(origin.y) || 0,
            Number(origin.yaw) || 0,
            Array.isArray(costmap.dynamic_obstacles) ? costmap.dynamic_obstacles.length : 0,
            Array.isArray(costmap.inflation) ? costmap.inflation.length : 0,
        ].join(':');
    }

    // An event timestamp is not a geometry revision. Use exact cell content
    // and origin comparisons so only identical WebGL geometry is reused.
    function costmapGeometryKey(costmap) {
        if (!costmap || typeof costmap !== 'object') return 'empty';
        const origin = costmap.origin || {};
        return [
            Number(costmap.width) || 0,
            Number(costmap.height) || 0,
            Number(costmap.resolution) || 0,
            Number(origin.x) || 0,
            Number(origin.y) || 0,
            Number(origin.yaw) || 0,
        ].join(':');
    }

    function sameCostmapIndices(snapshot, indices) {
        if (!Array.isArray(snapshot) || !Array.isArray(indices)) return false;
        if (snapshot.length !== indices.length) return false;
        for (let i = 0; i < snapshot.length; i += 1) {
            if (snapshot[i] !== indices[i]) return false;
        }
        return true;
    }

    function rebuildCostmapCells(rootGroup, costmap, indices, options) {
        clearGroup(rootGroup);
        if (!costmap || !Array.isArray(indices) || !indices.length) return;
        const width = Number(costmap.width) || 0;
        const height = Number(costmap.height) || 0;
        const resolution = Number(costmap.resolution) || 0;
        const total = width * height;
        if (width <= 0 || height <= 0 || resolution <= 0 || total <= 0) return;
        const valid = indices.filter(index => Number.isInteger(index) && index >= 0 && index < total);
        if (!valid.length) return;

        const geometry = new THREE.BoxGeometry(
            resolution * options.cellScale,
            resolution * options.cellScale,
            options.height,
        );
        const material = new THREE.MeshStandardMaterial({
            color: options.color,
            transparent: options.opacity < 1,
            opacity: options.opacity,
            roughness: 0.9,
            metalness: 0.01,
            depthWrite: options.depthWrite,
        });
        const mesh = new THREE.InstancedMesh(geometry, material, valid.length);
        mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
        mesh.frustumCulled = false;
        mesh.renderOrder = options.renderOrder;
        const matrix = new THREE.Matrix4();
        valid.forEach((index, instance) => {
            const x = index % width;
            const y = Math.floor(index / width);
            matrix.makeTranslation(
                (x + 0.5) * resolution,
                (y + 0.5) * resolution,
                options.height / 2,
            );
            mesh.setMatrixAt(instance, matrix);
        });
        mesh.instanceMatrix.needsUpdate = true;
        rootGroup.add(mesh);
        const origin = costmap.origin || {};
        rootGroup.position.set(Number(origin.x) || 0, Number(origin.y) || 0, 0);
        rootGroup.rotation.z = Number(origin.yaw) || 0;
    }

    let drawnObstacleGeometryKey = null;
    let drawnInflationGeometryKey = null;
    let drawnObstacleIndices = null;
    let drawnInflationIndices = null;

    function rebuildCostmap(costmap, forceInflation = false) {
        currentCostmap = costmap || null;
        const nextKey = costmapKey(currentCostmap);
        const geometryKey = costmapGeometryKey(currentCostmap);
        const obstacles = currentCostmap?.dynamic_obstacles || [];
        const inflation = currentCostmap?.inflation || [];
        if (nextKey !== currentObstacleCostmapKey) {
            currentObstacleCostmapKey = nextKey;
            if (geometryKey !== drawnObstacleGeometryKey
                || !sameCostmapIndices(drawnObstacleIndices, obstacles)) {
                rebuildCostmapCells(
                    dynamicObstacleRoot, currentCostmap, obstacles,
                    {
                        height: DYNAMIC_OBSTACLE_HEIGHT_M,
                        color: COLORS.dynamicObstacle,
                        opacity: 0.96,
                        cellScale: 0.92,
                        depthWrite: true,
                        renderOrder: 25,
                    },
                );
                drawnObstacleGeometryKey = geometryKey;
                drawnObstacleIndices = Array.isArray(obstacles)
                    ? obstacles.slice() : null;
            }
        }
        if (!layerGroups.inflation.visible) return;
        if (!forceInflation && nextKey === currentInflationCostmapKey) return;
        currentInflationCostmapKey = nextKey;
        if (forceInflation || geometryKey !== drawnInflationGeometryKey
            || !sameCostmapIndices(drawnInflationIndices, inflation)) {
            rebuildCostmapCells(
                inflationRoot, currentCostmap, inflation,
                {
                    height: INFLATION_HEIGHT_M,
                    color: COLORS.inflation,
                    opacity: 0.17,
                    cellScale: 0.98,
                    depthWrite: false,
                    renderOrder: 10,
                },
            );
            drawnInflationGeometryKey = geometryKey;
            drawnInflationIndices = Array.isArray(inflation)
                ? inflation.slice() : null;
        }
    }

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
            roughness: 0.56,
            metalness: 0.32,
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
                roughness: 0.97,
                metalness: 0.00,
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
        cameraTextureContext.fillStyle = '#0b1420';
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
                opacity: 0.90,
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
            // A flat contact shadow costs no realtime shadow-map pass.
            const contactShadow = new THREE.Mesh(
                new THREE.CircleGeometry(1, 32),
                new THREE.MeshBasicMaterial({
                    color: 0x02060b,
                    transparent: true,
                    opacity: 0.23,
                    depthWrite: false,
                }),
            );
            contactShadow.name = 'robot-contact-shadow';
            contactShadow.scale.set(chassisLength * 0.58, chassisWidth * 0.65, 1);
            contactShadow.position.z = 0.007;
            model.add(contactShadow);

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

            obstacleHeightM = clamp(wheelRadius * 6.75, 0.21, 0.36);

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

        // Scan geometry is frozen at the received frame's world pose in
        // rebuildScan(), not dragged along with interpolated robot movement.
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
                opacity: 0.26,
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
                size: viewerExpanded ? 0.038 : 0.048,
                sizeAttenuation: true,
                transparent: true,
                opacity: 1.0,
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
        const mount = scan?.mount;
        if (mount && [mount.x, mount.y, mount.yaw].every(v => Number.isFinite(Number(v)))) {
            scanMountLocal = {
                x: Number(mount.x),
                y: Number(mount.y),
                yaw: Number(mount.yaw),
            };
        }
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

        // Scan-time map->laser TF takes precedence over latest map->base_link.
        // Never apply the laser mount a second time to a sensor-frame TF.
        const matched = scan?.map_sensor_pose;
        const hasMatchedTf = Boolean(
            matched && matched.frame_id === 'map'
            && [matched.x, matched.y, matched.yaw].every(v => Number.isFinite(Number(v)))
        );
        // An unmatched LiDAR scan may serve as a temporary local preview.
        // It must not appear as a world obstacle on an authoritative SLAM map.
        const isPreview = !currentMap || currentMap.preview === true
            || currentMap.map_kind === 'preview';
        const canUseLatestPose = isPreview && Boolean(targetPose);
        if (hasMatchedTf) {
            for (const group of [scanRoot, pointsRoot]) {
                group.position.set(Number(matched.x), Number(matched.y), Number(matched.z) || 0);
                group.rotation.z = Number(matched.yaw);
            }
        } else if (canUseLatestPose) {
            const yaw = Number(targetPose.yaw) || 0;
            const cy = Math.cos(yaw);
            const sy = Math.sin(yaw);
            const sensorX = targetPose.x + cy * scanMountLocal.x - sy * scanMountLocal.y;
            const sensorY = targetPose.y + sy * scanMountLocal.x + cy * scanMountLocal.y;
            for (const group of [scanRoot, pointsRoot]) {
                group.position.set(sensorX, sensorY, targetPose.z);
                group.rotation.z = yaw + scanMountLocal.yaw;
            }
        }

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
        scanRoot.visible = Boolean(validCount && (hasMatchedTf || canUseLatestPose));
        pointsRoot.visible = Boolean(validCount && (hasMatchedTf || canUseLatestPose));
    }

    function displayedGlobalPath(
        control = currentControlState,
        _state = currentVisualizationState,
    ) {
        // The control API owns the Goal and its currently approved path.
        // /plan and /global_path visualize independent ROS computations and
        // do not carry an action ID: their delayed WS packets may describe
        // the preceding Goal after cancel/replacement. Never let those
        // unowned packets override the server's Goal-bound planned_path.
        if (String(control?.navigation_mode || '').toUpperCase() !== 'DRIVING'
            || !control?.active_goal) return [];

        const navState = String(control.navigation_state || '').toUpperCase();
        // A retained Goal may be retried after FAILED / EMERGENCY_STOPPED,
        // but its old path must not look executable. REPLACING_GOAL retains
        // the old Goal internally until Pi STOP has been confirmed.
        // A preview is NOT the FollowPath action's executed path. Until a
        // trustworthy execution-to-path binding exists, never render the
        // preview as though it were the actively followed route.
        if (['NAVIGATING', 'RESUMING'].includes(navState)) {
            // The Controller's accepted FollowPath path is not the preview.
            // Missing, stale or unowned execution paths are deliberately hidden.
            const appliedPath = control.executed_path;
            return Array.isArray(appliedPath) && appliedPath.length >= 2
                ? appliedPath : [];
        }
        if (!['PATH_READY', 'READY'].includes(navState)) {
            return [];
        }
        const authorizedPath = control.planned_path;
        return Array.isArray(authorizedPath) && authorizedPath.length >= 2
            ? authorizedPath
            : [];
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
            // Distinguish paths with identical XY but different yaw/motion
            // (reverse and in-place rotation are visible path changes).
            hash ^= Math.round((Number(point?.yaw) || 0) * 1000);
            hash = Math.imul(hash, 16777619);
            for (const letter of String(point?.motion || '')) {
                hash ^= letter.charCodeAt(0);
                hash = Math.imul(hash, 16777619);
            }
        }
        return `${path.length}:${hash >>> 0}`;
    }

    function rebuildPath(path) {
        const nextKey = pathKey(path);
        if (nextKey === currentPathKey) return;
        currentPathKey = nextKey;

        clearGroup(pathRoot);
        pathRoot.visible = Array.isArray(path) && path.length >= 2;
        if (!pathRoot.visible) return;

        const positions = [];
        for (const point of path) {
            const x = Number(point?.x);
            const y = Number(point?.y);
            if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
            // A ground-adjacent display elevation does not change ROS path
            // coordinates. With depth testing, chassis and real obstacle
            // meshes occlude the corresponding visual segments naturally.
            positions.push(x, y, 0.026);
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
                opacity: 1.0,
                depthTest: true,
                depthWrite: false,
            }),
        );
        line.frustumCulled = false;
        line.renderOrder = 20;

        const points = new THREE.Points(
            geometry,
            new THREE.PointsMaterial({
                color: COLORS.globalPath,
                size: viewerExpanded ? 0.038 : 0.060,
                sizeAttenuation: true,
                transparent: true,
                opacity: 0.90,
                depthTest: true,
                depthWrite: false,
            }),
        );
        points.frustumCulled = false;
        points.renderOrder = 21;
        pathRoot.add(line, points);

        // Arrowheads show the motion direction; reverse segments are amber.
        // Rotation-only segments receive a compact circular marker.
        const valid = path.filter(point =>
            Number.isFinite(Number(point?.x))
            && Number.isFinite(Number(point?.y))
        );
        // The existing heading/segment arrow glyphs follow the same
        // near-floor depth as the continuous path, never above the chassis.
        const height = 0.038;
        let distanceSinceArrow = 0;
        let lastRotationAt = null;
        for (let i = 0; i + 1 < valid.length; i += 1) {
            const from = valid[i];
            const to = valid[i + 1];
            const dx = Number(to.x) - Number(from.x);
            const dy = Number(to.y) - Number(from.y);
            const length = Math.hypot(dx, dy);
            const motion = String(from.motion || '');
            if (motion.startsWith('rotate_') && length < 0.003) {
                const center = new THREE.Vector3(Number(from.x), Number(from.y), height);
                if (lastRotationAt && center.distanceTo(lastRotationAt) < 0.08) continue;
                lastRotationAt = center.clone();
                const ring = new THREE.Mesh(
                    new THREE.RingGeometry(0.045, 0.06, 20),
                    new THREE.MeshBasicMaterial({
                        color: COLORS.globalPath,
                        side: THREE.DoubleSide,
                        depthTest: true,
                        depthWrite: false,
                    }),
                );
                ring.position.copy(center);
                ring.renderOrder = 22;
                pathRoot.add(ring);
                const left = motion === 'rotate_left';
                const tangent = new THREE.Vector3(left ? -1 : 1, 0, 0);
                const rotationArrow = new THREE.ArrowHelper(
                    tangent,
                    new THREE.Vector3(center.x, center.y + 0.06, height),
                    0.085, COLORS.globalPath, 0.045, 0.035,
                );
                rotationArrow.traverse(part => {
                    if (part.material) {
                        part.material.depthTest = true;
                        part.material.depthWrite = false;
                    }
                    part.renderOrder = 23;
                });
                pathRoot.add(rotationArrow);
                continue;
            }
            if (length < 0.001) continue;
            distanceSinceArrow += length;
            if (distanceSinceArrow < 0.28) continue;
            distanceSinceArrow = 0;
            const forward = motion !== 'reverse';
            const direction = new THREE.Vector3(dx / length, dy / length, 0);
            const position = new THREE.Vector3(
                (Number(from.x) + Number(to.x)) / 2,
                (Number(from.y) + Number(to.y)) / 2,
                height,
            );
            const arrow = new THREE.ArrowHelper(
                direction, position, 0.14,
                forward ? COLORS.globalPath : COLORS.trajectory,
                0.06, 0.035,
            );
            arrow.traverse(part => {
                if (part.material) {
                    part.material.depthTest = true;
                    part.material.depthWrite = false;
                }
                part.renderOrder = 22;
            });
            pathRoot.add(arrow);
        }
    }

    function createGoalMarker(goal, color, isDraft = false) {
        const x = Number(goal?.x);
        const y = Number(goal?.y);
        if (!Number.isFinite(x) || !Number.isFinite(y)) return null;

        const yaw = Number(goal?.yaw) || 0;
        const marker = new THREE.Group();
        marker.position.set(x, y, 0.045);
        marker.rotation.z = yaw;

        if (isDraft) {
            // Purely graphic dashed ring: not a tolerance or collision radius.
            const vertices = [];
            for (let index = 0; index < 64; index += 1) {
                const angle = 2 * Math.PI * index / 64;
                vertices.push(new THREE.Vector3(
                    0.095 * Math.cos(angle), 0.095 * Math.sin(angle), 0,
                ));
            }
            const ring = new THREE.LineLoop(
                new THREE.BufferGeometry().setFromPoints(vertices),
                new THREE.LineDashedMaterial({
                    color, dashSize: 0.038, gapSize: 0.027,
                    transparent: true, opacity: 0.98, depthWrite: false,
                }),
            );
            ring.computeLineDistances();
            marker.add(ring);
        } else {
            // Two concentric rings visually distinguish an active goal.
            for (const [inner, outer] of [[0.072, 0.099], [0.040, 0.052]]) {
                const ring = new THREE.Mesh(
                    new THREE.RingGeometry(inner, outer, 48),
                    new THREE.MeshBasicMaterial({
                        color, side: THREE.DoubleSide,
                        transparent: true, opacity: 0.98, depthWrite: false,
                    }),
                );
                marker.add(ring);
            }
        }

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
        const loopGoals = currentControlState?.loop_goals || [];
        const namedGoals = window.dabomLoopSessionState?.goals || [];
        const draftLoop = currentLoopDraft || [];
        const loopKey = points => points.map(goalKey).join('|');
        const nextKey = [
            goalKey(currentControlState?.active_goal),
            goalKey(currentDraftGoal),
            loopKey(loopGoals),
            loopKey(draftLoop),
            namedGoals.map(goal => goal.name + ':' + goalKey(goal.pose)).join('|'),
        ].join('::');
        if (nextKey === currentGoalKey) return;
        currentGoalKey = nextKey;

        clearGroup(goalRoot);
        const replacing = String(
            currentControlState?.navigation_state || ''
        ).toUpperCase() === 'REPLACING_GOAL';
        const loop = draftLoop.length ? draftLoop : loopGoals;
        if (loop.length) {
            const a = createGoalMarker(loop[0], COLORS.draftGoal, true);
            if (a) goalRoot.add(a);
            if (loop[1]) {
                const b = createGoalMarker(loop[1], COLORS.goal);
                if (b) goalRoot.add(b);
            }
        } else {
            const active = createGoalMarker(
                replacing ? null : currentControlState?.active_goal,
                COLORS.goal,
            );
            if (active) goalRoot.add(active);
        }
        for (const item of namedGoals) {
            const marker = createGoalMarker(item.pose, COLORS.goal);
            if (!marker) continue;
            const canvas = document.createElement('canvas');
            canvas.width = 96;
            canvas.height = 64;
            const ctx = canvas.getContext('2d');
            if (ctx) {
                ctx.fillStyle = '#f8fafc';
                ctx.font = 'bold 44px sans-serif';
                ctx.textAlign = 'center';
                ctx.fillText(item.name, 48, 47);
                const texture = new THREE.CanvasTexture(canvas);
                const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
                    map: texture, transparent: true, depthTest: false,
                }));
                sprite.position.z = 0.15;
                sprite.scale.set(0.23, 0.15, 1);
                marker.add(sprite);
            }
            goalRoot.add(marker);
        }
        const draft = createGoalMarker(currentDraftGoal, COLORS.draftGoal, true);
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
                opacity: 0.98,
                depthTest: true,
                depthWrite: false,
            }),
        );
        line.frustumCulled = false;
        line.renderOrder = 12;

        const points = new THREE.Points(
            trajectoryGeometry,
            new THREE.PointsMaterial({
                color: COLORS.trajectory,
                size: viewerExpanded ? 0.048 : 0.065,
                sizeAttenuation: true,
                transparent: true,
                opacity: 0.98,
                depthTest: true,
                depthWrite: false,
            }),
        );
        points.frustumCulled = false;
        points.renderOrder = 13;
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
        trajectoryFilteredPose = null;
        lastTrajectorySampleAt = 0;
        trajectorySessionActive = active;
        if (active && validPose(seedPose)) {
            trajectoryFilteredPose = {
                x: Number(seedPose.x),
                y: Number(seedPose.y),
            };
            trajectorySamples.push({ ...trajectoryFilteredPose });
            lastTrajectorySampleAt = performance.now();
        }
        renderTrajectory();
    }

    function sampleTrajectory(pose) {
        if (!trajectorySessionActive || !validPose(pose)) return;

        const raw = { x: Number(pose.x), y: Number(pose.y) };
        if (!trajectoryFilteredPose) {
            trajectoryFilteredPose = { ...raw };
        } else {
            const rawJump = Math.hypot(
                raw.x - trajectoryFilteredPose.x,
                raw.y - trajectoryFilteredPose.y,
            );
            if (rawJump > TRAJECTORY_MAX_JUMP_M) {
                // A localization reset must not draw a fake diagonal segment.
                resetTrajectory(pose, true);
                return;
            }
            trajectoryFilteredPose = {
                x: trajectoryFilteredPose.x
                    + (raw.x - trajectoryFilteredPose.x) * TRAJECTORY_FILTER_ALPHA,
                y: trajectoryFilteredPose.y
                    + (raw.y - trajectoryFilteredPose.y) * TRAJECTORY_FILTER_ALPHA,
            };
        }

        const next = { ...trajectoryFilteredPose };
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
            && (control?.nav2_ready || document.getElementById('loop-goal-toggle')?.checked === true)
            && !control?.emergency_stop
            && control?.connected === true
        );
    }

    function syncGoalInteractionAvailability(control = currentControlState) {
        const allowed = goalInteractionAllowed(control);
        for (const kind of ['set-goal', 'set-loop']) {
            const button = root.querySelector(`[data-lidar-interaction="${kind}"]`);
            if (!button) continue;
            button.disabled = !allowed;
            button.setAttribute('aria-disabled', String(!allowed));
            button.title = allowed
                ? (kind === 'set-loop'
                    ? 'A와 B를 차례로 지정하여 반복 주행 경로를 만듭니다.'
                    : '지도에서 Goal 위치와 방향을 지정합니다.')
                : 'DRIVING 및 Pi·Localization·Nav2 준비 후 사용할 수 있습니다.';
        }
        if (!allowed && ['set-goal', 'set-loop'].includes(interactionMode)) {
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
        rebuildPath(displayedGlobalPath(control, currentVisualizationState));
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

        // Preserve the recorded trace for review after completion but stop
        // appending stationary localization jitter after the Goal ends.
        if (!movingNow && window.dabomLoopSessionState?.playing !== true
            && window.dabomLoopSessionState?.recording !== true) trajectorySessionActive = false;
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
            ['set-goal', 'set-loop'].includes(mode) && goalInteractionAllowed()
                ? mode
                : 'view'
        );
        controls.enabled = viewerExpanded;
        controls.enableRotate = viewerExpanded && interactionMode === 'view';
        controls.enablePan = viewerExpanded && interactionMode === 'view';
        controls.enableZoom = viewerExpanded;
        root.closest('.lidar-viewer-overlay')?.classList.toggle(
            'goal-input-active',
            viewerExpanded && ['set-goal', 'set-loop'].includes(interactionMode),
        );
        root.querySelectorAll('[data-lidar-interaction]').forEach(button => {
            button.classList.toggle(
                'active',
                button.dataset.lidarInteraction === interactionMode,
            );
        });
        if (interactionMode !== 'view') canvas.focus({ preventScroll: true });
        document.dispatchEvent(new CustomEvent('dabom:navigation-interaction-mode', {
            detail: { mode: interactionMode },
        }));
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
        // Mapping must display the SLAM map and raw LiDAR, never stale
        // dynamic obstacles from a previous Driving Costmap.
        const liveCostmap = mappingMode ? null : (state.costmap || null);

        rebuildMap(liveMap, liveMap ? state.mapRevision : null);
        rebuildCostmap(liveCostmap);
        rebuildPath(displayedGlobalPath(currentControlState, state));

        // Before slam_toolbox exposes map->base_link, keep the RC car visible at
        // the mapping origin. As soon as the real TF pose arrives it replaces this
        // preview pose and the existing interpolation takes over.
        const visualizationPose = validPose(livePose)
            ? livePose
            : (mappingMode ? MAPPING_PREVIEW_POSE : null);

        // Keep the TF axes visible at the mapping origin until the real
        // map->base_link transform is available. The first real pose replaces
        // this preview immediately.
        const tfVisualizationPose = validPose(livePose)
            ? livePose
            : (mappingMode ? MAPPING_PREVIEW_POSE : null);
        applyTfPose(tfVisualizationPose);
        updateRobotPose(visualizationPose);
        // An older robot pose must not be applied to a newer scan as if
        // both came from the same TF frame. The next fresh pair restores
        // the scan automatically; the map and approved path stay unchanged.
        const scanReceived = Number(liveScan?.received_at);
        const poseReceived = Number(livePose?.received_at);
        const scanPoseMismatch = Boolean(
            validPose(livePose)
            && Number.isFinite(scanReceived) && scanReceived > 0
            && Number.isFinite(poseReceived) && poseReceived > 0
            && Math.abs(scanReceived - poseReceived) > 0.75
        );
        rebuildScan(scanPoseMismatch && !liveScan?.map_sensor_pose ? null : liveScan);

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

    document.addEventListener('dabom:loop-session-state', () => {
        rebuildGoals();
        const running = window.dabomLoopSessionState?.playing === true
            || window.dabomLoopSessionState?.recording === true;
        if (running && !trajectorySessionActive) {
            resetTrajectory(currentVisualizationState?.pose, true);
        } else if (!running && !['NAVIGATING', 'RESUMING'].includes(previousNavigationState)) {
            trajectorySessionActive = false;
        }
    });
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
            if (input.dataset.lidarDisplay === 'inflation') {
                if (input.checked) {
                    rebuildCostmap(currentCostmap, true);
                } else {
                    clearGroup(inflationRoot);
                    currentInflationCostmapKey = null;
                }
            }
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

    function applyTechnicalDensity() {
        // Appearance only: keep all geometry, poses, FOV and Orbit settings.
        const compact = !viewerExpanded;
        for (const grid of gridRoot.children) {
            if (grid.material) grid.material.opacity = compact ? 0.10 : 0.18;
        }
        for (const child of pathRoot.children) {
            if (child.isPoints) child.material.size = compact ? 0.060 : 0.038;
        }
        for (const child of trajectoryRoot.children) {
            if (child.isPoints) child.material.size = compact ? 0.065 : 0.048;
        }
        for (const child of pointsRoot.children) {
            if (child.isPoints) child.material.size = compact ? 0.048 : 0.038;
        }
    }

    function resize() {
        applyTechnicalDensity();
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
    document.addEventListener('dabom:navigation-loop-draft', event => {
        currentLoopDraft = Array.isArray(event.detail) ? event.detail : [];
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
        clearGroup(dynamicObstacleRoot);
        clearGroup(inflationRoot);
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

    rebuildGrid(null);
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
