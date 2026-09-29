
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const root = document.getElementById('lidar-3d-viewer');
const canvas = document.getElementById('lidar-3d-canvas');

if (root && canvas) {
    const renderer = new THREE.WebGLRenderer({
        canvas,
        antialias: true,
        alpha: false,
        powerPreference: 'high-performance',
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    renderer.setClearColor(0x101821, 1);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x101821);

    const camera = new THREE.PerspectiveCamera(48, 1, 0.02, 200);
    camera.position.set(4.8, -5.8, 5.2);

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

    const stageGrid = new THREE.GridHelper(12, 24, 0x64748b, 0x334155);
    stageGrid.material.transparent = true;
    stageGrid.material.opacity = 0.28;
    stageGrid.material.depthWrite = false;
    stageGrid.rotation.x = Math.PI / 2;
    layerGroups.grid.add(stageGrid);

    const stageFloor = new THREE.Mesh(
        new THREE.PlaneGeometry(12, 12),
        new THREE.MeshBasicMaterial({
            color: 0x18232d,
            transparent: true,
            opacity: 0.55,
            depthWrite: false,
        }),
    );
    stageFloor.position.z = -0.002;
    layerGroups.map.add(stageFloor);

    let viewMode = 'free';
    let followTarget = null;

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
            const offset = controls.target.clone().sub(followTarget);
            if (offset.lengthSq() > 1e-10) {
                camera.position.sub(offset);
                controls.target.copy(followTarget);
            }
        }

        controls.update();
        renderer.render(scene, camera);
        requestAnimationFrame(animate);
    }

    const observer = new ResizeObserver(resize);
    observer.observe(root);

    window.addEventListener('beforeunload', () => {
        observer.disconnect();
        controls.dispose();
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

    resize();
    animate();
}
