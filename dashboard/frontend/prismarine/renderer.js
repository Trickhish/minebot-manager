(function () {
  const params = new URLSearchParams(window.location.search);
  const botId = params.get("bot");
  const range = Number(params.get("range") || "40");
  const refreshMs = Math.max(1000, Number(params.get("refresh") || "30000"));
  const statusEl = document.getElementById("status");
  const POSE_MS = 150;
  const PLAYER_HEIGHT = 1.62;
  const NON_CUBE = /torch|sign|hopper|chest|water|lava|ladder|rail|vine|candle|chain|rod/;

  let renderer, scene, camera, worldGroup;
  let pose = null;

  function status(msg) { statusEl.textContent = msg; }
  function nameOf(entry) {
    const last = entry[entry.length - 1];
    return typeof last === "string" ? last.replace(/^minecraft:/, "") : "";
  }
  function stateOffset(entry) {
    if (typeof entry[entry.length - 1] !== "string") return -1;
    if (entry.length >= 8 && typeof entry[entry.length - 2] === "number") return entry[entry.length - 2];
    if (entry.length === 5 && typeof entry[3] === "number") return entry[3];
    return -1;
  }
  function colorOf(entry, alpha) {
    const c = new THREE.Color((entry[0] << 16) | (entry[1] << 8) | entry[2]);
    return new THREE.MeshLambertMaterial({ color: c, transparent: alpha < 1, opacity: alpha });
  }
  function fixedColor(hex, alpha) {
    return new THREE.MeshLambertMaterial({ color: hex, transparent: alpha < 1, opacity: alpha });
  }
  function isOpaqueName(name) {
    return name && !NON_CUBE.test(name) && name !== "glass" && !name.includes("leaves");
  }
  function decode(payload) {
    const [nx, ny, nz] = payload.dims;
    const grid = new Uint16Array(nx * ny * nz);
    let p = 0;
    for (let i = 0; i < payload.rle.length; i += 2) {
      const count = payload.rle[i], value = payload.rle[i + 1];
      if (value !== 0) grid.fill(value, p, p + count);
      p += count;
    }
    return grid;
  }
  function facingTurns(name, off) {
    if (name.includes("wall_torch") || name.includes("wall_sign")) return [2, 0, 1, 3][Math.max(0, off) % 4] || 0;
    if (name.includes("sign")) return Math.round((Math.max(0, off) % 16) / 4) % 4;
    if (name.includes("chest")) return [2, 0, 1, 3][Math.max(0, off) % 4] || 0;
    if (name === "hopper") return [0, 0, 0, 1, 2, 3][Math.max(0, off) % 6] || 0;
    return 0;
  }
  function rotateY(obj, turns) { obj.rotation.y = turns * Math.PI / 2; }
  function addBox(group, mat, x0, y0, z0, x1, y1, z1) {
    const geo = new THREE.BoxGeometry(x1 - x0, y1 - y0, z1 - z0);
    const mesh = new THREE.Mesh(geo, mat);
    mesh.position.set((x0 + x1) / 2 - 0.5, (y0 + y1) / 2 - 0.5, (z0 + z1) / 2 - 0.5);
    group.add(mesh);
  }
  function addTorch(group, name, turns) {
    const wood = fixedColor(0x9a6a32, 1);
    const flame = fixedColor(name.includes("soul") ? 0x5ecbff : name.includes("redstone") ? 0xff2e24 : 0xffd45a, 1);
    const stick = new THREE.Mesh(new THREE.CylinderGeometry(0.045, 0.045, 0.72, 6), wood);
    stick.position.y = -0.08;
    const light = new THREE.Mesh(new THREE.BoxGeometry(0.18, 0.16, 0.18), flame);
    light.position.y = 0.33;
    group.add(stick, light);
    if (name.includes("wall_torch")) {
      group.rotation.z = -Math.PI * 0.18;
      group.position.z = -0.22;
      rotateY(group, turns);
    }
  }
  function addSign(group, name, turns) {
    const mat = fixedColor(name.includes("bamboo") ? 0xcda95b : 0xa9743a, 1);
    addBox(group, mat, 0.08, 0.38, 0.45, 0.92, 0.78, 0.53);
    if (name.includes("wall_sign")) {
      group.position.z = -0.43;
    } else {
      addBox(group, mat, 0.47, 0.0, 0.47, 0.53, 0.42, 0.53);
    }
    rotateY(group, turns);
  }
  function addChest(group, turns) {
    const mat = fixedColor(0x9b652b, 1);
    const latch = fixedColor(0xd9c071, 1);
    addBox(group, mat, 0.06, 0.0, 0.06, 0.94, 0.88, 0.94);
    addBox(group, latch, 0.44, 0.36, 0.0, 0.56, 0.58, 0.08);
    rotateY(group, turns);
  }
  function addHopper(group, turns) {
    const mat = fixedColor(0x4d5156, 1);
    addBox(group, mat, 0.05, 0.62, 0.05, 0.95, 1.0, 0.95);
    addBox(group, mat, 0.18, 0.35, 0.18, 0.82, 0.7, 0.82);
    addBox(group, mat, 0.38, 0.0, 0.38, 0.62, 0.42, 0.62);
    addBox(group, mat, 0.38, 0.2, 0.0, 0.62, 0.45, 0.42);
    rotateY(group, turns);
  }
  function addSpecial(parent, wx, wy, wz, entry) {
    const name = nameOf(entry), off = stateOffset(entry);
    const group = new THREE.Group();
    group.position.set(wx + 0.5, wy + 0.5, wz + 0.5);
    if (name === "water") addBox(group, fixedColor(0x3f76e4, 0.72), 0, 0, 0, 1, 0.9, 1);
    else if (name === "lava") addBox(group, fixedColor(0xff6f19, 0.85), 0, 0, 0, 1, 0.9, 1);
    else if (name.includes("torch")) addTorch(group, name, facingTurns(name, off));
    else if (name.includes("sign")) addSign(group, name, facingTurns(name, off));
    else if (name.includes("chest")) addChest(group, facingTurns(name, off));
    else if (name === "hopper") addHopper(group, facingTurns(name, off));
    else addBox(group, colorOf(entry, 1), 0.1, 0, 0.1, 0.9, 0.9, 0.9);
    parent.add(group);
  }
  function build(payload) {
    const [nx, ny, nz] = payload.dims, [ox, oy, oz] = payload.origin;
    const pal = payload.palette, grid = decode(payload);
    const names = pal.map(nameOf);
    const opaque = pal.map(e => isOpaqueName(nameOf(e)));
    const at = (x, y, z) => x < 0 || y < 0 || z < 0 || x >= nx || y >= ny || z >= nz ? 0 : grid[(y * nz + z) * nx + x];
    const visible = (x, y, z) => !opaque[at(x + 1, y, z)] || !opaque[at(x - 1, y, z)] || !opaque[at(x, y + 1, z)] || !opaque[at(x, y - 1, z)] || !opaque[at(x, y, z + 1)] || !opaque[at(x, y, z - 1)];
    const next = new THREE.Group();
    const buckets = new Map();
    let special = 0, cubes = 0;

    for (let y = 0; y < ny; y++) for (let z = 0; z < nz; z++) for (let x = 0; x < nx; x++) {
      const idx = at(x, y, z);
      if (!idx) continue;
      const wx = ox + x, wy = oy + y, wz = oz + z;
      if (!opaque[idx]) {
        addSpecial(next, wx, wy, wz, pal[idx]);
        special++;
      } else if (visible(x, y, z)) {
        const key = `${pal[idx][0]},${pal[idx][1]},${pal[idx][2]}`;
        if (!buckets.has(key)) buckets.set(key, { entry: pal[idx], positions: [] });
        buckets.get(key).positions.push([wx + 0.5, wy + 0.5, wz + 0.5]);
        cubes++;
      }
    }
    const box = new THREE.BoxGeometry(1, 1, 1);
    const matrix = new THREE.Matrix4();
    for (const bucket of buckets.values()) {
      const mesh = new THREE.InstancedMesh(box, colorOf(bucket.entry, 1), bucket.positions.length);
      bucket.positions.forEach((p, i) => {
        matrix.makeTranslation(p[0], p[1], p[2]);
        mesh.setMatrixAt(i, matrix);
      });
      next.add(mesh);
    }
    scene.remove(worldGroup);
    worldGroup = next;
    scene.add(worldGroup);
    pose = { eye: payload.eye, yaw: payload.yaw, pitch: payload.pitch };
    status(`Prismarine-style · ${cubes} cubes · ${special} shaped`);
  }
  async function refreshWorld() {
    if (!botId) return status("missing bot id");
    try {
      const res = await fetch(`../api/bots/${encodeURIComponent(botId)}/voxels?radius=${range}&up=${range}&down=${range}`, { cache: "no-store" });
      if (!res.ok) throw new Error(await res.text());
      build(await res.json());
    } catch (e) {
      status(`renderer error: ${e.message || e}`);
    }
  }
  async function refreshPose() {
    try {
      const res = await fetch(`../api/bots/${encodeURIComponent(botId)}`, { cache: "no-store" });
      if (!res.ok) return;
      const bot = await res.json(), p = bot.position;
      if (!p || p.x == null || !pose) return;
      pose.eye = [p.x, p.y + PLAYER_HEIGHT, p.z];
      pose.yaw = p.yaw ?? pose.yaw;
      pose.pitch = p.pitch ?? pose.pitch;
    } catch {}
  }
  function resize() {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
  }
  function animate() {
    requestAnimationFrame(animate);
    if (pose) {
      const yaw = THREE.MathUtils.degToRad(pose.yaw), pitch = THREE.MathUtils.degToRad(pose.pitch);
      camera.position.set(pose.eye[0], pose.eye[1], pose.eye[2]);
      camera.rotation.set(pitch, yaw + Math.PI, 0, "YXZ");
    }
    renderer.render(scene, camera);
  }
  function init() {
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x87addb);
    scene.add(new THREE.AmbientLight(0xd8d8d8, 0.7));
    const sun = new THREE.DirectionalLight(0xffffff, 0.65);
    sun.position.set(1, 2, 0.5);
    scene.add(sun);
    worldGroup = new THREE.Group();
    scene.add(worldGroup);
    camera = new THREE.PerspectiveCamera(75, window.innerWidth / window.innerHeight, 0.05, Math.max(180, range * 3));
    renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(window.innerWidth, window.innerHeight);
    document.body.appendChild(renderer.domElement);
    window.addEventListener("resize", resize);
    refreshWorld();
    setInterval(refreshWorld, refreshMs);
    setInterval(refreshPose, POSE_MS);
    animate();
  }
  init();
})();
