// three_mill.js — Production Three.js 3D Factory Viewer & Simulation Engine
// Integrates UDYAM-TWIN Blender Factory Model (factory_scene.glb / udyam-twin-factory.glb)
// Featuring 7 synchronized 120-frame animation tracks, OrbitControls with safety boundaries,
// interactive raycasting object selection, machine highlighting, and Apple-inspired aesthetic.

let threeRenderer = null;
let threeScene = null;
let threeCamera = null;
let orbitControls = null;
let animationId = null;
let animClock = null;
let animMixer = null;
let activeActions = [];
let isSimPlaying = true;
let currentMachineId = "FACTORY";
let currentModelSource = "loading";
let cameraTween = null;
let raycaster = null;
let mouseCoords = null;
let interactiveMeshes = [];
let machineMeshMap = {
  "SAW-01": [],
  "LATHE-01": [],
  "VMC-01": [],
  "COMP-01": []
};
let originalMaterials = new Map();

// Camera Presets derived directly from Blender 5.2 coordinates
// Camera Presets derived directly from Blender 5.2 coordinates
// (Blender: X=Right, Y=Depth, Z=Up -> Three.js: X=Right, Y=Up, Z=-Y_depth)
const CAMERA_PRESET_CONFIGS = {
  FACTORY: {
    name: "Complete Factory Floor",
    pos: [-12.0, 9.5, 17.5],
    target: [-2.0, 1.5, -1.0]
  },
  FRONT: {
    name: "Main Entrance (Gate View)",
    pos: [-8.8, 2.6, 13.5],
    target: [-8.8, 1.8, 0.0]
  },
  TOP: {
    name: "Overhead Floorplan",
    pos: [-2.0, 22.0, -1.0],
    target: [-2.0, 0.0, -1.0]
  },
  "SAW-01": {
    name: "Bar-Cutting Saw (SAW-01)",
    pos: [-4.2, 2.2, 1.6],
    target: [-6.7, 0.9, -0.8]
  },
  "LATHE-01": {
    name: "CNC Turning Lathe (LATHE-01)",
    pos: [-2.1, 2.0, 2.2],
    target: [-2.05, 1.0, -1.3]
  },
  "VMC-01": {
    name: "CNC Milling Machine (VMC-01)",
    pos: [2.2, 2.8, 3.2],
    target: [3.6, 1.6, -1.1]
  },
  "COMP-01": {
    name: "Rotary Air Compressor (COMP-01)",
    pos: [-7.6, 2.6, -2.4],
    target: [-8.9, 2.0, -6.0]
  }
};

function getCameraPreset(key) {
  const cfg = CAMERA_PRESET_CONFIGS[key] || CAMERA_PRESET_CONFIGS.FACTORY;
  return {
    name: cfg.name,
    pos: (typeof THREE !== "undefined") ? new THREE.Vector3(cfg.pos[0], cfg.pos[1], cfg.pos[2]) : { x: cfg.pos[0], y: cfg.pos[1], z: cfg.pos[2] },
    target: (typeof THREE !== "undefined") ? new THREE.Vector3(cfg.target[0], cfg.target[1], cfg.target[2]) : { x: cfg.target[0], y: cfg.target[1], z: cfg.target[2] }
  };
}

// Procedural fallback animation targets
let fallbackAnimElements = [];

function disposeThree() {
  if (animationId) {
    cancelAnimationFrame(animationId);
    animationId = null;
  }
  if (cameraTween) {
    cancelAnimationFrame(cameraTween);
    cameraTween = null;
  }
  if (animMixer) {
    animMixer.stopAllAction();
    animMixer = null;
  }
  activeActions = [];
  if (orbitControls) {
    orbitControls.dispose();
    orbitControls = null;
  }
  if (threeScene) {
    threeScene.traverse((child) => {
      if (child.isMesh) {
        if (child.geometry) child.geometry.dispose();
        if (child.material) {
          if (Array.isArray(child.material)) {
            child.material.forEach((m) => m && m.dispose && m.dispose());
          } else if (child.material.dispose) {
            child.material.dispose();
          }
        }
      }
    });
  }
  if (threeRenderer) {
    threeRenderer.dispose();
    const canvas = threeRenderer.domElement;
    if (canvas && canvas.parentNode) canvas.parentNode.removeChild(canvas);
    threeRenderer = null;
  }
  threeScene = null;
  threeCamera = null;
  currentMachineId = "FACTORY";
  currentModelSource = "none";
  interactiveMeshes = [];
  machineMeshMap = { "SAW-01": [], "LATHE-01": [], "VMC-01": [], "COMP-01": [] };
  originalMaterials.clear();
  fallbackAnimElements = [];
}

function updateModelSourceBadge(text, isGlb) {
  const badge = document.getElementById("threeModelBadge");
  if (badge) {
    badge.textContent = text;
    badge.style.color = isGlb ? "var(--status-normal-text, #30D158)" : "var(--status-attention-text, #FF9F0A)";
  }
}

// Apple-style cubic ease-out camera interpolation
function smoothMoveCamera(toPos, toTarget, durationMs = 850) {
  if (!threeCamera || !orbitControls) return;
  const startPos = threeCamera.position.clone();
  const startTarget = orbitControls.target.clone();
  const startTime = performance.now();

  function step(now) {
    const elapsed = now - startTime;
    const progress = Math.min(elapsed / durationMs, 1);
    const t = 1 - Math.pow(1 - progress, 3);

    threeCamera.position.lerpVectors(startPos, toPos, t);
    orbitControls.target.lerpVectors(startTarget, toTarget, t);
    orbitControls.update();

    if (progress < 1) {
      cameraTween = requestAnimationFrame(step);
    } else {
      cameraTween = null;
    }
  }

  if (cameraTween) cancelAnimationFrame(cameraTween);
  cameraTween = requestAnimationFrame(step);
}

// Master Initialization of Three.js Viewport
function initThreeView(initialTarget = "FACTORY") {
  // If scene already initialized, just switch camera
  if (threeScene && threeCamera && orbitControls) {
    window.focusCameraOnMachine(initialTarget);
    return;
  }

  disposeThree();

  const wrapper = document.getElementById("threeCanvasWrapper");
  if (!wrapper) return;

  if (typeof THREE === "undefined") {
    wrapper.innerHTML = '<div style="padding:48px; text-align:center; color:#A1A1A6; font-size:14px;">Three.js library is loading. Please check network connection or reload.</div>';
    return;
  }

  wrapper.innerHTML = "";

  const canvas = document.createElement("canvas");
  canvas.id = "threeCanvas";
  canvas.style.width = "100%";
  canvas.style.height = "100%";
  canvas.style.display = "block";
  wrapper.appendChild(canvas);

  const width = Math.max(wrapper.clientWidth || 0, 960);
  const height = Math.max(wrapper.clientHeight || 0, 560);

  threeRenderer = new THREE.WebGLRenderer({
    canvas,
    antialias: true,
    alpha: true,
    powerPreference: "high-performance"
  });
  threeRenderer.setSize(width, height);
  threeRenderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  if (THREE.sRGBEncoding) {
    threeRenderer.outputEncoding = THREE.sRGBEncoding;
  }
  if (THREE.ACESFilmicToneMapping) {
    threeRenderer.toneMapping = THREE.ACESFilmicToneMapping;
    threeRenderer.toneMappingExposure = 1.05;
  }

  threeScene = new THREE.Scene();
  threeCamera = new THREE.PerspectiveCamera(45, width / height, 0.1, 1000);

  // Set default factory overview position
  const defaultPreset = getCameraPreset(initialTarget);
  threeCamera.position.copy(defaultPreset.pos);

  // OrbitControls with strict safety limits for older owners
  orbitControls = new THREE.OrbitControls(threeCamera, threeRenderer.domElement);
  orbitControls.enableDamping = true;
  orbitControls.dampingFactor = 0.05;
  orbitControls.target.copy(defaultPreset.target);
  orbitControls.minDistance = 1.5;
  orbitControls.maxDistance = 42.0;
  orbitControls.minPolarAngle = 0.05; // overhead clamp
  orbitControls.maxPolarAngle = Math.PI / 2 - 0.04; // ground clamp (~87deg) prevents camera from sinking below floor

  // Photometric Real-Time Lighting Setup
  setupSceneLighting();

  // Raycaster & Mouse coordinates for interaction
  raycaster = new THREE.Raycaster();
  mouseCoords = new THREE.Vector2();

  // Animation Clock
  animClock = new THREE.Clock();

  // Load Blender Model with graceful procedural fallback
  loadBlenderModel(initialTarget);

  // Setup Interaction Handlers
  setupInteractionEvents(canvas);

  // Render Loop
  function animate() {
    animationId = requestAnimationFrame(animate);

    const delta = animClock ? animClock.getDelta() : 0.016;

    // Advance real Blender GLB animations
    if (animMixer && isSimPlaying) {
      animMixer.update(delta);
    }

    // Advance procedural fallback elements if active
    if (fallbackAnimElements.length > 0 && isSimPlaying) {
      for (const el of fallbackAnimElements) {
        el.mesh.rotation[el.axis] += el.speed;
      }
    }

    if (orbitControls) orbitControls.update();
    if (threeRenderer && threeScene && threeCamera) {
      threeRenderer.render(threeScene, threeCamera);
    }
  }
  animate();
}

// -----------------------------------------------------------------------------
// Lighting System matching Blender EEVEE / Photometric Daylight
// -----------------------------------------------------------------------------
function setupSceneLighting() {
  // Soft ambient fill
  const ambient = new THREE.AmbientLight(0xffffff, 0.95);
  threeScene.add(ambient);

  // Main Key Sun Light (Warm industrial skylight)
  const sunKey = new THREE.DirectionalLight(0xfffaee, 1.25);
  sunKey.position.set(12, 22, 16);
  threeScene.add(sunKey);

  // Cool Sky Fill Light
  const fillCool = new THREE.DirectionalLight(0xdce8ff, 0.65);
  fillCool.position.set(-14, 8, -12);
  threeScene.add(fillCool);

  // High-Bay LED fixture washes
  const bayNorth = new THREE.DirectionalLight(0xffffff, 0.55);
  bayNorth.position.set(-6, 8, 2);
  threeScene.add(bayNorth);

  const baySouth = new THREE.DirectionalLight(0xffffff, 0.55);
  baySouth.position.set(3, 8, 2);
  threeScene.add(baySouth);

  // Bottom bounce light to illuminate machine undersides
  const bounceLight = new THREE.DirectionalLight(0xffffff, 0.35);
  bounceLight.position.set(0, -6, 0);
  threeScene.add(bounceLight);

  // Precision Industrial Ground Grid
  const gridHelper = new THREE.GridHelper(36, 36, 0x5a6578, 0x222630);
  gridHelper.position.y = -0.01;
  threeScene.add(gridHelper);
}

// -----------------------------------------------------------------------------
// GLTF Loader & Animation Mixer Integration
// -----------------------------------------------------------------------------
function loadBlenderModel(initialTarget) {
  updateModelSourceBadge("Loading Blender 3D Model...", true);

  const candidateUrls = ["/assets/3d/factory_scene.glb"];  // served by the backend from assets/3d

  if (typeof THREE.GLTFLoader !== "undefined") {
    const loader = new THREE.GLTFLoader();
    let attemptIdx = 0;

    function tryLoadNext() {
      if (attemptIdx >= candidateUrls.length) {
        console.warn("[Three.js] All GLB URLs exhausted. Building procedural digital twin.");
        buildProceduralFallback();
        if (initialTarget && initialTarget !== "FACTORY") {
          window.focusCameraOnMachine(initialTarget);
        }
        return;
      }

      const url = candidateUrls[attemptIdx++];
      loader.load(
        url,
        (gltf) => {
          const model = gltf.scene;
          threeScene.add(model);
          currentModelSource = "blender_glb";
          updateModelSourceBadge("Blender Photorealistic GLB (Active)", true);

          // Process animations
          if (gltf.animations && gltf.animations.length > 0) {
            animMixer = new THREE.AnimationMixer(model);
            activeActions = [];
            gltf.animations.forEach((clip) => {
              const action = animMixer.clipAction(clip);
              action.setLoop(THREE.LoopRepeat);
              action.play();
              activeActions.push(action);
            });
            const animBadge = document.getElementById("simStatusPill");
            if (animBadge) animBadge.textContent = `● 4 Stations Active · ${gltf.animations.length} Action Loops`;
          }

          // Map meshes to machine IDs and store original materials
          indexModelMeshes(model);

          // Focus initial target if specified
          if (initialTarget && initialTarget !== "FACTORY") {
            window.focusCameraOnMachine(initialTarget);
          }
        },
        undefined,
        (err) => {
          console.warn(`[Three.js] Notice loading ${url}:`, err);
          tryLoadNext();
        }
      );
    }

    tryLoadNext();
  } else {
    buildProceduralFallback();
  }
}

// Index model objects into interactive machine categories
function indexModelMeshes(model) {
  interactiveMeshes = [];
  machineMeshMap = { "SAW-01": [], "LATHE-01": [], "VMC-01": [], "COMP-01": [] };
  originalMaterials.clear();

  model.traverse((child) => {
    if (child.isMesh) {
      const name = (child.name || "").toUpperCase();

      // Determine machine ownership
      let machineId = null;
      if (name.includes("SAW")) machineId = "SAW-01";
      else if (name.includes("LATHE")) machineId = "LATHE-01";
      else if (name.includes("VMC")) machineId = "VMC-01";
      else if (name.includes("COMP")) machineId = "COMP-01";

      if (machineId) {
        child.userData.machineId = machineId;
        machineMeshMap[machineId].push(child);
        interactiveMeshes.push(child);

        // Cache original material for reset
        if (child.material) {
          originalMaterials.set(child, child.material);
        }
      }
    }
  });
}

// -----------------------------------------------------------------------------
// Interactive Raycasting & Machine Highlighting
// -----------------------------------------------------------------------------
function setupInteractionEvents(canvas) {
  let isDragging = false;
  let downPos = { x: 0, y: 0 };

  canvas.addEventListener("pointerdown", (e) => {
    isDragging = false;
    downPos = { x: e.clientX, y: e.clientY };
  });

  canvas.addEventListener("pointermove", (e) => {
    const dist = Math.hypot(e.clientX - downPos.x, e.clientY - downPos.y);
    if (dist > 5) isDragging = true;

    if (!threeCamera || interactiveMeshes.length === 0) return;

    const rect = canvas.getBoundingClientRect();
    mouseCoords.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    mouseCoords.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;

    raycaster.setFromCamera(mouseCoords, threeCamera);
    const intersects = raycaster.intersectObjects(interactiveMeshes, false);

    if (intersects.length > 0) {
      const targetMesh = intersects[0].object;
      const machineId = targetMesh.userData.machineId;
      if (machineId) {
        canvas.style.cursor = "pointer";
        showCanvasTooltip(e.clientX, e.clientY, machineId);
        return;
      }
    }
    canvas.style.cursor = "grab";
    hideCanvasTooltip();
  });

  canvas.addEventListener("pointerup", (e) => {
    if (isDragging) return; // ignore orbit drags

    if (!threeCamera || interactiveMeshes.length === 0) return;

    const rect = canvas.getBoundingClientRect();
    mouseCoords.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    mouseCoords.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;

    raycaster.setFromCamera(mouseCoords, threeCamera);
    const intersects = raycaster.intersectObjects(interactiveMeshes, false);

    if (intersects.length > 0) {
      const machineId = intersects[0].object.userData.machineId;
      if (machineId) {
        if (typeof window.selectMachineIn3D === "function") {
          window.selectMachineIn3D(machineId);
        } else {
          window.focusCameraOnMachine(machineId);
        }
      }
    }
  });

  canvas.addEventListener("pointerleave", () => {
    canvas.style.cursor = "default";
    hideCanvasTooltip();
  });
}

function showCanvasTooltip(x, y, machineId) {
  let tip = document.getElementById("canvasHoverTooltip");
  if (!tip) {
    tip = document.createElement("div");
    tip.id = "canvasHoverTooltip";
    tip.style.position = "fixed";
    tip.style.pointerEvents = "none";
    tip.style.padding = "6px 12px";
    tip.style.background = "rgba(20, 20, 24, 0.94)";
    tip.style.color = "#FFFFFF";
    tip.style.fontSize = "12px";
    tip.style.fontWeight = "600";
    tip.style.borderRadius = "8px";
    tip.style.border = "1px solid rgba(255, 255, 255, 0.18)";
    tip.style.boxShadow = "0 4px 16px rgba(0, 0, 0, 0.4)";
    tip.style.zIndex = "9999";
    tip.style.transition = "opacity 0.12s ease";
    document.body.appendChild(tip);
  }
  const nameMap = {
    "SAW-01": "Station 1 · Bar-Cutting Saw",
    "LATHE-01": "Station 2 · CNC Slant Lathe",
    "VMC-01": "Station 3 · CNC Milling Machine",
    "COMP-01": "Utility · Rotary Air Compressor"
  };
  tip.textContent = `${nameMap[machineId] || machineId} (Click to inspect)`;
  tip.style.left = `${x + 14}px`;
  tip.style.top = `${y + 14}px`;
  tip.style.opacity = "1";
}

function hideCanvasTooltip() {
  const tip = document.getElementById("canvasHoverTooltip");
  if (tip) tip.style.opacity = "0";
}

// Machine Highlighting (Subtle Apple-grade luminance glow on active station)
function highlightMachineInScene(selectedId) {
  Object.keys(machineMeshMap).forEach((id) => {
    const meshes = machineMeshMap[id];
    const isSelected = id === selectedId;

    meshes.forEach((mesh) => {
      const origMat = originalMaterials.get(mesh);
      if (!origMat) return;

      if (isSelected) {
        if (origMat.emissive) {
          mesh.material = origMat.clone();
          mesh.material.emissive = new THREE.Color(0x1a3848);
          mesh.material.emissiveIntensity = 0.55;
        }
      } else {
        mesh.material = origMat;
      }
    });
  });
}

// -----------------------------------------------------------------------------
// Camera Navigation Controls
// -----------------------------------------------------------------------------
window.focusCameraOnMachine = function (targetKey) {
  if (!threeCamera || !orbitControls) return;

  currentMachineId = targetKey;
  const preset = getCameraPreset(targetKey);

  smoothMoveCamera(preset.pos, preset.target, 850);
  highlightMachineInScene(targetKey === "FACTORY" ? null : targetKey);

  // Update Viewport HUD Info
  const titleEl = document.getElementById("viewportMachineTitle");
  if (titleEl) {
    titleEl.textContent = preset.name;
  }
};

window.focusThreeMachine = window.focusCameraOnMachine;

window.resetThreeView = function () {
  window.focusCameraOnMachine("FACTORY");
};

window.showFactoryOverview = function () {
  window.focusCameraOnMachine("FACTORY");
};

// -----------------------------------------------------------------------------
// Animation Playback Controls (Play / Pause / Reset)
// -----------------------------------------------------------------------------
window.toggleSimPlayback = function () {
  isSimPlaying = !isSimPlaying;
  const btn = document.getElementById("btnPlayPause");
  if (btn) {
    btn.innerHTML = isSimPlaying ? "⏸ Pause" : "▶ Resume";
    btn.setAttribute("title", isSimPlaying ? "Pause 3D Kinematics" : "Resume 3D Kinematics");
  }
  return isSimPlaying;
};

window.resetSimPlayback = function () {
  if (animMixer) {
    animMixer.setTime(0);
  }
  if (!isSimPlaying) {
    window.toggleSimPlayback();
  }
};

window.toggleThreeAnimation = window.toggleSimPlayback;

// -----------------------------------------------------------------------------
// High-Fidelity Procedural Fallback Builder
// -----------------------------------------------------------------------------
function buildProceduralFallback() {
  updateModelSourceBadge("Procedural Digital Twin (Active)", false);
  currentModelSource = "procedural";
  fallbackAnimElements = [];
  interactiveMeshes = [];
  machineMeshMap = { "SAW-01": [], "LATHE-01": [], "VMC-01": [], "COMP-01": [] };

  // Floor
  const floorGeom = new THREE.PlaneGeometry(28, 16);
  const floorMat = new THREE.MeshStandardMaterial({ color: 0x16181d, roughness: 0.85, metalness: 0.2 });
  const floor = new THREE.Mesh(floorGeom, floorMat);
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = -0.01;
  threeScene.add(floor);

  // Green thoroughfare walkway
  const walkGeom = new THREE.PlaneGeometry(22, 2.2);
  const walkMat = new THREE.MeshStandardMaterial({ color: 0x059499, roughness: 0.6 });
  const walkway = new THREE.Mesh(walkGeom, walkMat);
  walkway.rotation.x = -Math.PI / 2;
  walkway.position.set(-1.0, 0.002, 1.8);
  threeScene.add(walkway);

  // Station 1: SAW-01
  const sawGroup = new THREE.Group();
  sawGroup.position.set(-6.7, 0, -0.8);
  const sawBase = new THREE.Mesh(new THREE.BoxGeometry(2.2, 0.6, 1.2), new THREE.MeshStandardMaterial({ color: 0x242a38, metalness: 0.7 }));
  sawBase.position.y = 0.3;
  sawGroup.add(sawBase);
  const sawBow = new THREE.Mesh(new THREE.BoxGeometry(1.6, 0.35, 0.35), new THREE.MeshStandardMaterial({ color: 0xd97706, metalness: 0.8 }));
  sawBow.position.set(0, 0.85, 0);
  sawGroup.add(sawBow);
  sawGroup.userData.machineId = "SAW-01";
  sawBase.userData.machineId = "SAW-01";
  sawBow.userData.machineId = "SAW-01";
  threeScene.add(sawGroup);
  interactiveMeshes.push(sawBase, sawBow);
  machineMeshMap["SAW-01"].push(sawBase, sawBow);
  fallbackAnimElements.push({ mesh: sawBow, axis: "z", speed: 0.02 });

  // Station 2: LATHE-01
  const latheGroup = new THREE.Group();
  latheGroup.position.set(-2.05, 0, -1.3);
  const latheBase = new THREE.Mesh(new THREE.BoxGeometry(2.6, 0.7, 1.4), new THREE.MeshStandardMaterial({ color: 0x1f242e, metalness: 0.7 }));
  latheBase.position.y = 0.35;
  latheGroup.add(latheBase);
  const latheChuck = new THREE.Mesh(new THREE.CylinderGeometry(0.32, 0.32, 0.15, 32), new THREE.MeshStandardMaterial({ color: 0xd0d5dd, metalness: 0.9 }));
  latheChuck.rotation.z = Math.PI / 2;
  latheChuck.position.set(-0.5, 0.85, 0);
  latheGroup.add(latheChuck);
  latheBase.userData.machineId = "LATHE-01";
  latheChuck.userData.machineId = "LATHE-01";
  threeScene.add(latheGroup);
  interactiveMeshes.push(latheBase, latheChuck);
  machineMeshMap["LATHE-01"].push(latheBase, latheChuck);
  fallbackAnimElements.push({ mesh: latheChuck, axis: "x", speed: 0.06 });

  // Station 3: VMC-01
  const vmcGroup = new THREE.Group();
  vmcGroup.position.set(3.6, 0, -1.1);
  const vmcBase = new THREE.Mesh(new THREE.BoxGeometry(2.4, 0.65, 2.0), new THREE.MeshStandardMaterial({ color: 0x222733, metalness: 0.7 }));
  vmcBase.position.y = 0.32;
  vmcGroup.add(vmcBase);
  const vmcCol = new THREE.Mesh(new THREE.BoxGeometry(0.8, 2.2, 0.8), new THREE.MeshStandardMaterial({ color: 0x181d28, metalness: 0.6 }));
  vmcCol.position.set(0, 1.35, -0.5);
  vmcGroup.add(vmcCol);
  const vmcSpindle = new THREE.Mesh(new THREE.CylinderGeometry(0.1, 0.1, 0.45, 24), new THREE.MeshStandardMaterial({ color: 0xd4af37, metalness: 0.9 }));
  vmcSpindle.position.set(0, 1.4, 0.1);
  vmcGroup.add(vmcSpindle);
  vmcBase.userData.machineId = "VMC-01";
  vmcCol.userData.machineId = "VMC-01";
  vmcSpindle.userData.machineId = "VMC-01";
  threeScene.add(vmcGroup);
  interactiveMeshes.push(vmcBase, vmcCol, vmcSpindle);
  machineMeshMap["VMC-01"].push(vmcBase, vmcCol, vmcSpindle);
  fallbackAnimElements.push({ mesh: vmcSpindle, axis: "y", speed: 0.08 });

  // Station 4: COMP-01
  const compGroup = new THREE.Group();
  compGroup.position.set(-8.9, 0, -6.0);
  const compTank = new THREE.Mesh(new THREE.CylinderGeometry(0.45, 0.45, 2.2, 32), new THREE.MeshStandardMaterial({ color: 0x1e293b, metalness: 0.8 }));
  compTank.rotation.z = Math.PI / 2;
  compTank.position.y = 0.6;
  compGroup.add(compTank);
  const compFan = new THREE.Mesh(new THREE.CylinderGeometry(0.2, 0.2, 0.06, 16), new THREE.MeshStandardMaterial({ color: 0x059499, metalness: 0.8 }));
  compFan.rotation.z = Math.PI / 2;
  compFan.position.set(-1.1, 0.8, 0);
  compGroup.add(compFan);
  compTank.userData.machineId = "COMP-01";
  compFan.userData.machineId = "COMP-01";
  threeScene.add(compGroup);
  interactiveMeshes.push(compTank, compFan);
  machineMeshMap["COMP-01"].push(compTank, compFan);
  fallbackAnimElements.push({ mesh: compFan, axis: "x", speed: 0.09 });
}

// -----------------------------------------------------------------------------
// Live Telemetry / RPM Visualization Hook
// -----------------------------------------------------------------------------
window.updateMachineVisualization = function (machineId, obs, twin) {
  if (!obs) return;
  const rpmBadge = document.getElementById("hudRpm");
  if (rpmBadge && typeof obs.rpm === "number") {
    rpmBadge.textContent = `${obs.rpm.toFixed(0)} RPM`;
  }
};

window.addEventListener("resize", () => {
  if (!threeCamera || !threeRenderer) return;
  const wrapper = document.getElementById("threeCanvasWrapper");
  if (!wrapper) return;
  const width = wrapper.clientWidth || 960;
  const height = wrapper.clientHeight || 560;
  threeCamera.aspect = width / height;
  threeCamera.updateProjectionMatrix();
  threeRenderer.setSize(width, height);
});

window.initThreeView = initThreeView;
window.disposeThree = disposeThree;
