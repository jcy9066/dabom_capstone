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
    };

    const DEFAULT_OCCUPIED_HEIGHT_M = 0.08;
    const MAP_OCCUPIED_THRESHOLD = 50;

    const renderer = new THREE.WebGLRenderer({
        canvas,
        antialias: true,
        alpha: false,
        powerPreference: 'high-performance',
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
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

    let currentMapKey = null;
    let currentMap = null;
    let obstacleHeightM = DEFAULT_OCCUPIED_HEIGHT_M;
    let robotModelReady = false;
    let robotVisual = null;
    let baseAxes = null;
    let viewMode = 'free';
    let followTarget = null;

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

    function updateRobotPose(pose) {
        const available = pose
            && Number.isFinite(Number(pose.x))
            && Number.isFinite(Number(pose.y));
        robotPoseGroup.visible = Boolean(available && robotModelReady);
        tfPoseGroup.visible = Boolean(available);
        if (!available) {
            followTarget = null;
            return;
        }

        const x = Number(pose.x);
        const y = Number(pose.y);
        const yaw = Number(pose.yaw) || 0;
        const z = 0.006;
        robotPoseGroup.position.set(x, y, z);
        robotPoseGroup.rotation.z = yaw;
        tfPoseGroup.position.set(x, y, z);
        tfPoseGroup.rotation.z = yaw;

        followTarget = new THREE.Vector3(x, y, z);
        if (viewMode === 'follow') {
            controls.target.copy(followTarget);
        }
    }

    function applyVisualizationState(state) {
        if (!state) return;
        rebuildMap(state.map || null, state.mapRevision);
        updateRobotPose(state.pose || null);
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

    function animate() {
        resize();

        if (viewMode === 'follow' && followTarget) {
            const delta = followTarget.clone().sub(controls.target);
            if (delta.lengthSq() > 1e-10) {
                camera.position.add(delta);
                controls.target.copy(followTarget);
            }
        }

        controls.update();
        renderer.render(scene, camera);
        requestAnimationFrame(animate);
    }

    const observer = new ResizeObserver(resize);
    observer.observe(root);

    document.addEventListener('dabom:navigation-visualization-state', event => {
        applyVisualizationState(event.detail);
    });

    window.addEventListener('beforeunload', () => {
        observer.disconnect();
        controls.dispose();
        clearGroup(mapRoot);
        clearGroup(gridRoot);
        clearGroup(robotPoseGroup);
        clearGroup(tfPoseGroup);
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
    resize();
    animate();
}
