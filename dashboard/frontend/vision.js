// Bot "vision": a first-person WebGL view of the block volume around the bot.
//
// The bot-host serializes the nearby blocks as a run-length encoded voxel grid
// + color palette (GET /api/bots/{id}/voxels); all rendering happens here in
// the browser. If the server has a texture atlas (built from a resource pack),
// palette entries also carry [top, side, bottom] atlas tile indices and we
// UV-map real textures onto the cube faces; otherwise we fall back to the flat
// per-block color. Zero dependencies -- raw WebGL, hand-rolled matrix math.

const Vision = (() => {
  const FOG_COLOR = [0.53, 0.68, 0.92];   // sky-ish; also the clear color
  const GEOM_REFRESH_MS = 1800;
  const FACE_SHADE = { top: 1.0, bottom: 0.5, north: 0.8, south: 0.8, east: 0.62, west: 0.62 };

  // Unit-cube faces: [name, 4 corner offsets (ccw from outside), neighbor delta].
  const FACES = [
    ["top",    [[0,1,0],[0,1,1],[1,1,1],[1,1,0]], [0, 1, 0]],
    ["bottom", [[0,0,1],[0,0,0],[1,0,0],[1,0,1]], [0,-1, 0]],
    ["south",  [[0,0,1],[1,0,1],[1,1,1],[0,1,1]], [0, 0, 1]],
    ["north",  [[1,0,0],[0,0,0],[0,1,0],[1,1,0]], [0, 0,-1]],
    ["east",   [[1,0,1],[1,0,0],[1,1,0],[1,1,1]], [1, 0, 0]],
    ["west",   [[0,0,0],[0,0,1],[0,1,1],[0,1,0]], [-1,0, 0]],
  ];
  const TRI = [0, 1, 2, 0, 2, 3];              // two triangles from 4 corners
  const FLOATS = 10, STRIDE = FLOATS * 4;      // pos3, color3, uv2, shade1, tex1

  let canvas, gl, prog, loc = {};
  let vbo, vertexCount = 0;
  let botId = null, range = 40;
  let pose = null;
  let geomTimer = null, rafHandle = null, fetching = false;

  // texture atlas (loaded once, shared across bots)
  let atlasState = "init";   // init | loading | loaded | none
  let atlasTex = null, atlasCols = 0, atlasRows = 0, atlasTile = 16;
  let atlasWaiters = [];

  function status(msg) {
    const el = document.getElementById("vision-status");
    if (el) el.textContent = msg;
  }

  // -- GL setup --------------------------------------------------------------
  function initGL() {
    if (gl) return true;
    canvas = document.getElementById("vision-canvas");
    gl = canvas.getContext("webgl", { antialias: true });
    if (!gl) { status("WebGL not available"); return false; }

    const vs = `
      attribute vec3 aPos; attribute vec3 aColor; attribute vec2 aUV;
      attribute float aShade; attribute float aTex;
      uniform mat4 uMVP; uniform vec3 uEye;
      varying vec3 vColor; varying vec2 vUV; varying float vShade, vTex, vDist;
      void main() {
        gl_Position = uMVP * vec4(aPos, 1.0);
        vColor = aColor; vUV = aUV; vShade = aShade; vTex = aTex;
        vDist = distance(aPos, uEye);
      }`;
    const fs = `
      precision mediump float;
      varying vec3 vColor; varying vec2 vUV; varying float vShade, vTex, vDist;
      uniform vec3 uFog; uniform float uFogFar; uniform sampler2D uAtlas;
      void main() {
        vec3 base = vTex > 0.5 ? texture2D(uAtlas, vUV).rgb : vColor;
        float f = clamp((vDist - uFogFar * 0.4) / (uFogFar * 0.6), 0.0, 1.0);
        gl_FragColor = vec4(mix(base * vShade, uFog, f), 1.0);
      }`;
    prog = link(compile(gl.VERTEX_SHADER, vs), compile(gl.FRAGMENT_SHADER, fs));
    gl.useProgram(prog);
    for (const a of ["aPos", "aColor", "aUV", "aShade", "aTex"]) loc[a] = gl.getAttribLocation(prog, a);
    for (const u of ["uMVP", "uEye", "uFog", "uFogFar", "uAtlas"]) loc[u] = gl.getUniformLocation(prog, u);
    gl.uniform3fv(loc.uFog, FOG_COLOR);
    gl.uniform1i(loc.uAtlas, 0);
    vbo = gl.createBuffer();
    gl.enable(gl.DEPTH_TEST);
    gl.disable(gl.CULL_FACE);
    gl.clearColor(FOG_COLOR[0], FOG_COLOR[1], FOG_COLOR[2], 1.0);
    return true;
  }

  function compile(type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  }
  function link(vs, fs) {
    const p = gl.createProgram();
    gl.attachShader(p, vs); gl.attachShader(p, fs); gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    return p;
  }

  // -- texture atlas ---------------------------------------------------------
  function ensureAtlas(cb) {
    if (atlasState === "loaded" || atlasState === "none") return cb();
    atlasWaiters.push(cb);
    if (atlasState === "loading") return;
    atlasState = "loading";
    const done = (s) => { atlasState = s; atlasWaiters.forEach(f => f()); atlasWaiters = []; };
    fetch("api/textures/atlas.json").then(r => r.json()).then(meta => {
      if (!meta.has_textures) return done("none");
      atlasCols = meta.cols; atlasRows = meta.rows; atlasTile = meta.tile;
      const img = new Image();
      img.onload = () => {
        atlasTex = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, atlasTex);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, img);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        done("loaded");
      };
      img.onerror = () => done("none");
      img.src = "api/textures/atlas.png";
    }).catch(() => done("none"));
  }

  function tileUV(tile) {
    const c = tile % atlasCols, r = (tile / atlasCols) | 0;
    const du = 0.5 / (atlasCols * atlasTile), dv = 0.5 / (atlasRows * atlasTile);
    const u0 = c / atlasCols + du, u1 = (c + 1) / atlasCols - du;
    const v0 = r / atlasRows + dv, v1 = (r + 1) / atlasRows - dv;
    return [[u0, v1], [u1, v1], [u1, v0], [u0, v0]];  // matches corner order
  }

  // -- geometry --------------------------------------------------------------
  function buildMesh(payload) {
    const [nx, ny, nz] = payload.dims;
    const [ox, oy, oz] = payload.origin;
    const pal = payload.palette;
    const textured = atlasState === "loaded";

    const grid = new Uint16Array(nx * ny * nz);   // order (y, z, x)
    let p = 0;
    for (let i = 0; i < payload.rle.length; i += 2) {
      const c = payload.rle[i], v = payload.rle[i + 1];
      if (v !== 0) grid.fill(v, p, p + c);
      p += c;
    }
    const at = (x, y, z) => (x < 0 || y < 0 || z < 0 || x >= nx || y >= ny || z >= nz)
      ? 0 : grid[(y * nz + z) * nx + x];

    const data = [];
    for (let y = 0; y < ny; y++)
      for (let z = 0; z < nz; z++)
        for (let x = 0; x < nx; x++) {
          const idx = grid[(y * nz + z) * nx + x];
          if (idx === 0) continue;
          const entry = pal[idx];
          const r = entry[0] / 255, g = entry[1] / 255, b = entry[2] / 255;
          for (const [name, corners, d] of FACES) {
            if (at(x + d[0], y + d[1], z + d[2]) !== 0) continue;   // hidden
            const shade = FACE_SHADE[name];
            let tile = -1;
            if (textured && entry.length > 3)
              tile = name === "top" ? entry[3] : name === "bottom" ? entry[5] : entry[4];
            const uv = tile >= 0 ? tileUV(tile) : null;
            const tex = uv ? 1 : 0;
            const wx = ox + x, wy = oy + y, wz = oz + z;
            for (const k of TRI) {
              const c = corners[k], t = uv ? uv[k] : [0, 0];
              data.push(wx + c[0], wy + c[1], wz + c[2], r, g, b, t[0], t[1], shade, tex);
            }
          }
        }

    const arr = new Float32Array(data);
    gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
    gl.bufferData(gl.ARRAY_BUFFER, arr, gl.DYNAMIC_DRAW);
    vertexCount = arr.length / FLOATS;
  }

  async function refreshGeometry() {
    if (!botId || fetching) return;
    fetching = true;
    try {
      const r = await fetch(`api/bots/${botId}/voxels?radius=${range}&up=${range}&down=${range}`);
      if (r.status !== 200) {
        status(r.status === 503 ? "waiting for chunks…" : `error ${r.status}`);
        return;
      }
      const payload = await r.json();
      buildMesh(payload);
      pose = { eye: payload.eye, yaw: payload.yaw, pitch: payload.pitch };
      status(`${vertexCount / 6 | 0} faces` + (atlasState === "loaded" ? " · textured" : ""));
    } catch (e) {
      status("error");
    } finally {
      fetching = false;
    }
  }

  // -- render loop -----------------------------------------------------------
  function syncSize() {
    if (!canvas) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(canvas.clientWidth * dpr));
    const h = Math.max(1, Math.round(canvas.clientHeight * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  }

  function frame() {
    rafHandle = requestAnimationFrame(frame);
    if (!gl) return;
    syncSize();  // keep the drawing buffer matched to the (possibly resized) canvas
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    if (!pose || vertexCount === 0) return;

    const proj = perspective(70 * Math.PI / 180, canvas.width / canvas.height, 0.1, (range + 4) * 1.8);
    const mvp = multiply(proj, viewMatrix(pose));

    gl.useProgram(prog);
    gl.uniformMatrix4fv(loc.uMVP, false, mvp);
    gl.uniform3fv(loc.uEye, pose.eye);
    gl.uniform1f(loc.uFogFar, range);
    if (atlasTex) { gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, atlasTex); }

    gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
    setAttr(loc.aPos, 3, 0); setAttr(loc.aColor, 3, 12); setAttr(loc.aUV, 2, 24);
    setAttr(loc.aShade, 1, 32); setAttr(loc.aTex, 1, 36);
    gl.drawArrays(gl.TRIANGLES, 0, vertexCount);
  }
  function setAttr(l, size, off) {
    gl.enableVertexAttribArray(l);
    gl.vertexAttribPointer(l, size, gl.FLOAT, false, STRIDE, off);
  }

  // -- math (column-major 4x4) ----------------------------------------------
  function forward(yaw, pitch) {
    const yr = yaw * Math.PI / 180, pr = pitch * Math.PI / 180, cp = Math.cos(pr);
    return [-Math.sin(yr) * cp, -Math.sin(pr), Math.cos(yr) * cp];
  }
  function viewMatrix(pose) {
    const eye = pose.eye, f = forward(pose.yaw, pose.pitch);
    let r = cross(f, [0, 1, 0]);
    r = normalize(r.some(v => v) ? r : [1, 0, 0]);
    const u = cross(r, f);
    return new Float32Array([
      r[0], u[0], -f[0], 0, r[1], u[1], -f[1], 0, r[2], u[2], -f[2], 0,
      -dot(r, eye), -dot(u, eye), dot(f, eye), 1,
    ]);
  }
  function perspective(fovy, aspect, near, far) {
    const t = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
    return new Float32Array([
      t / aspect, 0, 0, 0, 0, t, 0, 0, 0, 0, (far + near) * nf, -1, 0, 0, 2 * far * near * nf, 0,
    ]);
  }
  function multiply(a, b) {
    const o = new Float32Array(16);
    for (let c = 0; c < 4; c++)
      for (let r = 0; r < 4; r++) {
        let s = 0;
        for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k];
        o[c * 4 + r] = s;
      }
    return o;
  }
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  function normalize(v) { const l = Math.hypot(v[0], v[1], v[2]) || 1; return [v[0] / l, v[1] / l, v[2] / l]; }

  // -- public API ------------------------------------------------------------
  return {
    attach(id, r) {
      if (!initGL()) return;
      botId = id;
      if (r) range = r;
      pose = null; vertexCount = 0;
      status("loading…");
      ensureAtlas(() => {
        if (botId !== id) return;
        refreshGeometry();
        clearInterval(geomTimer);
        geomTimer = setInterval(refreshGeometry, GEOM_REFRESH_MS);
      });
      if (!rafHandle) frame();
    },
    detach() {
      botId = null; pose = null; vertexCount = 0;
      clearInterval(geomTimer); geomTimer = null;
      if (rafHandle) { cancelAnimationFrame(rafHandle); rafHandle = null; }
      if (gl) gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      status("disabled");
    },
    setRange(r) { range = r; if (botId) refreshGeometry(); },
    resize() { syncSize(); },
    setPose(p) {
      if (!botId || !pose || p == null || p.x == null) return;
      pose.eye = [p.x, p.y + 1.62, p.z];
      if (p.yaw != null) pose.yaw = p.yaw;
      if (p.pitch != null) pose.pitch = p.pitch;
    },
    isOn() { return botId !== null; },
  };
})();
