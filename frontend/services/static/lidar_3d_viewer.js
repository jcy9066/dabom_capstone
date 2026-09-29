import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { ThreeMFLoader } from 'three/addons/loaders/3MFLoader.js';

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
    const LIDAR_HEIGHT_M = 0.12;
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
    const CAMERA_MOUNT_Z_OFFSET_M = 0.03;

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
    let currentMap = null;
    let obstacleHeightM = DEFAULT_OCCUPIED_HEIGHT_M;
    let robotModelReady = false;
    let robotVisual = null;
    let baseAxes = null;
    let viewMode = 'free';
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
    let cameraMountLocal = new THREE.Vector3(0.12, 0, 0.16);
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
    const raycaster = new THREE.Raycaster();
    const groundPlane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);

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

    function rebuildMap(map, revision) {
        const nextKey = mapKey(map, revision);
        if (nextKey && nextKey === currentMapKey) return;

        clearGroup(mapRoot);
        currentMapKey = nextKey;
        currentMap = map || null;

        if (!map) {
            clearGroup(gridRoot);
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

        const transform = mapTransform(map);
        mapRoot.position.set(transform.x, transform.y, transform.z);
        mapRoot.rotation.z = transform.yaw;
        rebuildGrid(map);
    }

    function clamp(value, min, max) {
        return Math.min(max, Math.max(min, value));
    }

    function loadThreeMF(url) {
        const loader = new ThreeMFLoader();
        return new Promise((resolve, reject) => {
            loader.load(url, resolve, undefined, reject);
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

    function normalizeUnitScale(object) {
        const box = new THREE.Box3().setFromObject(object);
        const size = box.getSize(new THREE.Vector3());
        const maxDimension = Math.max(size.x, size.y, size.z);
        if (maxDimension > 5) {
            object.scale.multiplyScalar(0.001);
            object.updateMatrixWorld(true);
        }
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

    function createWheel(radius, width) {
        const wheel = new THREE.Mesh(
            new THREE.CylinderGeometry(radius, radius, width, 20),
            new THREE.MeshStandardMaterial({
                color: COLORS.wheel,
                roughness: 0.92,
                metalness: 0.02,
            }),
        );
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
        const uvs = new Float32Array([
            0, 0,
            1, 0,
            1, 1,
            0, 1,
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
                depthWrite: false,
            }),
        );
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
            }),
        );
        cameraViewPoseGroup.add(cameraViewPlane);

        cameraViewBorder = new THREE.LineLoop(
            createCameraOutlineGeometry(viewWidth, viewHeight),
            new THREE.LineBasicMaterial({
                color: COLORS.cameraViewBorder,
                transparent: true,
                opacity: 0.78,
                depthWrite: false,
            }),
        );
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
        cameraMountLocal.set(
            chassisLength * CAMERA_MOUNT_FORWARD_RATIO,
            0,
            topZ + CAMERA_MOUNT_Z_OFFSET_M,
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
    }

    async function buildRobotModel() {
        try {
            const [upper, lower] = await Promise.all([
                loadThreeMF('/static/assets/robot_upper_chassis.3mf'),
                loadThreeMF('/static/assets/robot_lower_chassis.3mf'),
            ]);

            normalizeUnitScale(lower);
            normalizeUnitScale(upper);
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
            cad.rotation.z = longAxisIsY ? -Math.PI / 2 : 0;

            const lowerBox = placePartAtBottom(lower, wheelRadius * 0.72);
            const upperBox = placePartAtBottom(upper, lowerBox.max.z + 0.012);
            cad.add(lower, upper);
            model.add(cad);

            const wheelX = chassisLength * 0.35;
            const wheelY = chassisWidth * 0.5 + wheelWidth * 0.18;
            for (const x of [-wheelX, wheelX]) {
                for (const y of [-wheelY, wheelY]) {
                    const wheel = createWheel(wheelRadius, wheelWidth);
                    wheel.position.set(x, y, wheelRadius);
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

            if (currentMap) {
                currentMapKey = null;
                rebuildMap(currentMap, window.dabomNavigationVisualizationState?.mapRevision);
            }
        } catch (error) {
            console.error('3D chassis load failed:', error);
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
        tfPoseGroup.visible = Boolean(visible);
        cameraFrustumPoseGroup.visible = Boolean(visible && cameraVisualReady);
        cameraViewPoseGroup.visible = Boolean(visible && cameraVisualReady);
        scanRoot.visible = Boolean(visible && scanRayGeometry);
        pointsRoot.visible = Boolean(visible && scanPointGeometry);
    }

    function applyRenderedPose(pose) {
        if (!pose) {
            setPoseVisibility(false);
            followTarget = null;
            return;
        }

        const groups = [
            robotPoseGroup,
            tfPoseGroup,
            cameraFrustumPoseGroup,
            cameraViewPoseGroup,
            scanRoot,
            pointsRoot,
        ];
        for (const group of groups) {
            group.position.set(pose.x, pose.y, pose.z);
            group.rotation.z = pose.yaw;
        }
        setPoseVisibility(true);

        followTarget = new THREE.Vector3(pose.x, pose.y, pose.z);
    }

    function updateRobotPose(pose) {
        const next = poseFromPayload(pose);
        targetPose = next;
        if (!next) {
            renderedPose = null;
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

        const distance = Math.hypot(
            targetPose.x - renderedPose.x,
            targetPose.y - renderedPose.y,
        );
        if (distance > 1.5) {
            renderedPose = { ...targetPose };
            applyRenderedPose(renderedPose);
            return;
        }

        const alpha = 1 - Math.exp(-12 * deltaSec);
        renderedPose.x += (targetPose.x - renderedPose.x) * alpha;
        renderedPose.y += (targetPose.y - renderedPose.y) * alpha;
        renderedPose.z += (targetPose.z - renderedPose.z) * alpha;

        let yawDelta = targetPose.yaw - renderedPose.yaw;
        yawDelta = Math.atan2(Math.sin(yawDelta), Math.cos(yawDelta));
        renderedPose.yaw += yawDelta * alpha;
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
            scanRayPositions[rayOffset + 2] = LIDAR_HEIGHT_M;
            scanRayPositions[rayOffset + 3] = x;
            scanRayPositions[rayOffset + 4] = y;
            scanRayPositions[rayOffset + 5] = LIDAR_HEIGHT_M;

            const pointOffset = validCount * 3;
            scanPointPositions[pointOffset] = x;
            scanPointPositions[pointOffset + 1] = y;
            scanPointPositions[pointOffset + 2] = LIDAR_HEIGHT_M;
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
        rebuildPath(control.planned_path || []);
        rebuildGoals();

        if (mode === 'MAPPING') {
            if (modeRestarted || trajectorySamples.length) {
                resetTrajectory(null, false);
            }
            trajectoryRoot.visible = false;
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
        interactionMode = mode === 'set-goal' ? 'set-goal' : 'view';
        controls.enableRotate = interactionMode === 'view';
        controls.enablePan = interactionMode === 'view';
        controls.enableZoom = true;
        root.closest('.lidar-viewer-overlay')?.classList.toggle(
            'goal-input-active',
            interactionMode === 'set-goal',
        );
        root.querySelectorAll('[data-lidar-interaction]').forEach(button => {
            button.classList.toggle(
                'active',
                button.dataset.lidarInteraction === interactionMode,
            );
        });
        if (interactionMode === 'set-goal') canvas.focus({ preventScroll: true });
    }

    function applyVisualizationState(state) {
        if (!state) return;
        currentVisualizationState = state;
        rebuildMap(state.map || null, state.mapRevision);
        updateRobotPose(state.pose || null);
        rebuildScan(state.scan || null);
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

    function setViewMode(mode) {
        viewMode = mode;
        setActiveViewButton(mode);

        if (mode === 'top') {
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
        if (mode === 'follow' && followTarget) {
            controls.target.copy(followTarget);
        }
        controls.update();
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
        if (viewMode === 'top') {
            viewMode = 'free';
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
        }
    }

    function animate(now = performance.now()) {
        interpolateRobotPose(now);

        if (viewMode === 'follow' && followTarget) {
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
        setInteractionMode,
        interactionMode() {
            return interactionMode;
        },
        screenToGround,
        setFollowTarget(target) {
            followTarget = target ? new THREE.Vector3(target.x, target.y, target.z || 0) : null;
            if (viewMode === 'follow' && followTarget) controls.target.copy(followTarget);
        },
        requestRender() {
            controls.update();
            renderer.render(scene, camera);
        },
    };

    buildRobotModel();
    applyVisualizationState(window.dabomNavigationVisualizationState);
    applyControlState(window.dabomNavigationControlState);
    setInteractionMode('view');
    resize();
    animate();
}
