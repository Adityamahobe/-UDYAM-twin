// Sign-in page background (web/login.html #loginBg): our own factory, assets/3d/factory_scene.glb, rendered with
// the vendored three.js. The camera circles the plant slowly while the model's machine animations loop (the same
// model and lighting as the Factory section's 3D view, web/three_mill.js). Decorative only: no controls, no input.
// If WebGL or the model is unavailable the factory photo behind the canvas stays visible.
(function () {
  const host = document.getElementById("loginBg");
  if (!host || !window.THREE || !THREE.GLTFLoader) return;
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "low-power" });
  } catch (_) {
    return;  // no WebGL: keep the photo
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
  if (THREE.sRGBEncoding) renderer.outputEncoding = THREE.sRGBEncoding;
  if (THREE.ACESFilmicToneMapping) { renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.05; }
  renderer.domElement.className = "login-3d";
  host.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 1000);
  // The hall spans x -15..15, z -9..9 with the roof at 6 m: the camera circles inside it on an ellipse, below the
  // cable trays (5.2 m), looking at the middle of the line
  const target = new THREE.Vector3(-1.0, 1.2, -0.5);
  const rx = 10.5, rz = 5.6, height = 3.6;

  scene.add(new THREE.AmbientLight(0xffffff, 0.95));
  [[0xfffaee, 1.25, [10, 18, 8]], [0xdce8ff, 0.65, [-12, 10, -6]], [0xffffff, 0.55, [0, 14, -16]],
   [0xffffff, 0.55, [0, 14, 16]], [0xffffff, 0.35, [0, -6, 0]]].forEach(([c, i, p]) => {
    const l = new THREE.DirectionalLight(c, i); l.position.set(...p); scene.add(l);
  });

  function resize() {
    const w = host.clientWidth || innerWidth, h = host.clientHeight || innerHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  addEventListener("resize", resize);
  resize();

  let mixer = null, angle = 2.2;  // start from the south-west, like the dashboard's overview
  const clock = new THREE.Clock();
  function place() {
    camera.position.set(target.x + Math.cos(angle) * rx, height, target.z + Math.sin(angle) * rz);
    camera.lookAt(target);
  }
  function frame() {
    const dt = Math.min(clock.getDelta(), 0.1);
    angle += dt * (2 * Math.PI / 120);  // one full circle every 2 minutes
    if (mixer) mixer.update(dt);
    place();
    renderer.render(scene, camera);
    requestAnimationFrame(frame);
  }

  new THREE.GLTFLoader().load("/assets/3d/factory_scene.glb", gltf => {
    scene.add(gltf.scene);
    if (!still && gltf.animations && gltf.animations.length) {
      mixer = new THREE.AnimationMixer(gltf.scene);
      gltf.animations.forEach(clip => { const a = mixer.clipAction(clip); a.setLoop(THREE.LoopRepeat); a.play(); });
    }
    host.classList.add("is-3d");  // fades the canvas in over the photo
    if (still) { place(); renderer.render(scene, camera); } else { clock.getDelta(); frame(); }
  }, undefined, () => { renderer.domElement.remove(); });  // model missing: keep the photo
})();
