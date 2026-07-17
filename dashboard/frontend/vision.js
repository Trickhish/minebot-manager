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
  const DEFAULT_GEOM_REFRESH_MS = 30000;
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
  const FULL_CUBE = [[[0, 0, 0], [1, 1, 1]]];
  const CROSSED_PLANES = [
    [[0.15, 0, 0.15], [0.85, 0, 0.85], [0.85, 1, 0.85], [0.15, 1, 0.15]],
    [[0.85, 0, 0.15], [0.15, 0, 0.85], [0.15, 1, 0.85], [0.85, 1, 0.15]],
  ];
  // Axis-aligned crossed planes (a '+' from above); the real torch texture is a
  // thin central stick, so only its centre column is opaque -- the wide plane is
  // cut out by alpha. Corner order is [bl, br, tr, tl] to match tileUV.
  const TORCH_PLANES = [
    [[0, 0, 0.5], [1, 0, 0.5], [1, 1, 0.5], [0, 1, 0.5]],
    [[0.5, 0, 0], [0.5, 0, 1], [0.5, 1, 1], [0.5, 1, 0]],
  ];
  // Wall torch: the standing torch, rigidly tilted back and seated on the wall.
  // A rigid rotation (not a shear) keeps the texture undistorted so the flame
  // stays exactly at the tip; poke-out past the cube is transparent.
  const WALL_TORCH_TILT = 24 * Math.PI / 180;   // lean from vertical
  const leanTorchCorner = (c) => {
    const ct = Math.cos(WALL_TORCH_TILT), st = Math.sin(WALL_TORCH_TILT);
    const y = c[1] * 0.82, z = c[2] - 0.5;      // scale height, pivot in z at centre
    return [c[0], (y * ct - z * st) + 0.13, (y * st + z * ct) + 0.06];
  };
  const WALL_TORCH_PLANES = TORCH_PLANES.map(p => p.map(leanTorchCorner));
  const PART_COLORS = {
    water: [0.25, 0.47, 0.9],
    lava: [1.0, 0.38, 0.08],
    torchWood: [0.58, 0.34, 0.13],
    torchFlame: [1.0, 0.78, 0.24],
    redstoneFlame: [1.0, 0.12, 0.08],
    soulFlame: [0.35, 0.82, 1.0],
    chestLatch: [0.85, 0.73, 0.36],
    hopperDark: [0.2, 0.22, 0.24],
    hopperLight: [0.39, 0.42, 0.45],
    signText: [0.17, 0.1, 0.04],
  };
  const PLANT_NAMES = new Set([
    "grass", "short_grass", "tall_grass", "fern", "large_fern", "dead_bush",
    "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet",
    "red_tulip", "orange_tulip", "white_tulip", "pink_tulip", "oxeye_daisy",
    "cornflower", "lily_of_the_valley", "wither_rose", "crimson_roots",
    "warped_roots", "nether_sprouts", "sugar_cane", "kelp", "kelp_plant",
    "wheat", "carrots", "potatoes", "beetroots", "nether_wart", "vine",
    "cave_vines", "cave_vines_plant", "sweet_berry_bush", "pink_petals",
    "wildflowers", "leaf_litter", "open_eyeblossom", "closed_eyeblossom",
  ]);

  // Terrain hidden by X-ray so ores/caves/structures show through. Only common
  // "filler" blocks -- ores and anything player-placed stay visible.
  const XRAY_HIDDEN = new Set([
    "stone", "cobblestone", "dirt", "grass_block", "coarse_dirt", "podzol",
    "rooted_dirt", "mud", "muddy_mangrove_roots", "clay", "gravel", "sand",
    "red_sand", "sandstone", "red_sandstone", "andesite", "diorite", "granite",
    "tuff", "deepslate", "cobbled_deepslate", "netherrack", "end_stone",
    "basalt", "smooth_basalt", "blackstone", "calcite", "dripstone_block",
    "magma_block", "soul_sand", "soul_soil", "snow_block", "packed_ice", "ice",
    "blue_ice", "moss_block", "mossy_cobblestone", "bedrock", "water", "lava",
    "sculk", "grass_block_snow", "dirt_path", "farmland",
  ]);
  // Containers highlighted through walls by the "highlight chests" tool.
  const HIGHLIGHT_NAMES = new Set([
    "chest", "trapped_chest", "ender_chest", "barrel", "hopper", "dropper",
    "dispenser", "furnace", "blast_furnace", "smoker", "brewing_stand",
  ]);
  const HIGHLIGHT_COLOR = [1.0, 0.82, 0.18];
  const isHighlight = (name) => HIGHLIGHT_NAMES.has(name)
    || name.endsWith("_shulker_box") || name === "shulker_box";

  let canvas, gl, prog, loc = {};
  let prismarineFrame = null;
  let vbo, vertexCount = 0;
  let highlightVbo, highlightCount = 0;
  let lastPayload = null;
  // Tools: freecam (detached fly camera), xray (see-through terrain), chests
  // (highlight containers through walls). Rebuild the mesh when xray/chests flip.
  const tools = { freecam: false, xray: false, chests: false };
  let freecamPose = null;
  let lookaroundPose = null;
  const freecamKeys = new Set();
  let botId = null, range = 40;
  let refreshMs = DEFAULT_GEOM_REFRESH_MS;
  let pose = null, poseTarget = null, lastFrameTime = 0;
  let controlActive = false;
  let geomTimer = null, rafHandle = null, fetching = false;
  let rendererMode = localStorage.getItem("visionRenderer") || "custom";

  // texture atlas (loaded once, shared across bots)
  let atlasState = "init";   // init | loading | loaded | none
  let atlasTex = null, atlasCols = 0, atlasRows = 0, atlasTile = 16, atlasStems = {};
  let atlasWaiters = [];

  function status(msg) {
    const el = document.getElementById("vision-status");
    if (el) el.textContent = msg;
  }

  function activeElement() {
    return rendererMode === "prismarine" && prismarineFrame ? prismarineFrame : canvas;
  }

  function ensurePrismarineFrame() {
    if (prismarineFrame) return prismarineFrame;
    prismarineFrame = document.createElement("iframe");
    prismarineFrame.id = "vision-prismarine";
    prismarineFrame.title = "Prismarine renderer";
    prismarineFrame.hidden = true;
    prismarineFrame.setAttribute("allow", "fullscreen; gamepad; pointer-lock");
    prismarineFrame.setAttribute("referrerpolicy", "same-origin");
    const view = document.querySelector(".vision-view");
    if (view) view.insertBefore(prismarineFrame, document.getElementById("vision-status"));
    return prismarineFrame;
  }

  function showCustomCanvas() {
    if (canvas) {
      canvas.hidden = false;
      canvas.style.display = "block";
    }
    if (prismarineFrame) {
      prismarineFrame.hidden = true;
      prismarineFrame.style.display = "none";
    }
  }

  function showPrismarineFrame() {
    if (canvas) {
      canvas.hidden = true;
      canvas.style.display = "none";
    }
    const frame = ensurePrismarineFrame();
    frame.hidden = false;
    frame.style.display = "block";
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
        vec3 base;
        if (vTex > 0.5) {
          vec4 tx = texture2D(uAtlas, vUV);
          if (tx.a < 0.5) discard;   // cut out transparent pixels (torches, plants, rails)
          base = tx.rgb;
        } else {
          base = vColor;
        }
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
    highlightVbo = gl.createBuffer();
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
    // Version tag busts any stale browser cache of the atlas when the tiles
    // change (e.g. new tints). Bump alongside the vision.js cache-buster.
    const av = "?v=20260717-inventory-items";
    fetch("api/textures/atlas.json" + av).then(r => r.json()).then(meta => {
      if (!meta.has_textures) return done("none");
      atlasCols = meta.cols; atlasRows = meta.rows; atlasTile = meta.tile;
      atlasStems = meta.stems || {};
      const img = new Image();
      img.onload = () => {
        atlasTex = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, atlasTex);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        done("loaded");
      };
      img.onerror = () => done("none");
      img.src = "api/textures/atlas.png" + av;
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
  function entryName(entry) {
    const last = entry[entry.length - 1];
    return typeof last === "string" ? last.replace(/^minecraft:/, "") : "";
  }

  function entryTile(entry, faceName, textured) {
    if (!textured || entry.length < 6 || typeof entry[3] !== "number") return -1;
    if (usesFlatColor(entryName(entry))) return -1;
    return faceName === "top" ? entry[3] : faceName === "bottom" ? entry[5] : entry[4];
  }

  function entryStateOffset(entry) {
    if (typeof entry[entry.length - 1] !== "string") return -1;
    if (entry.length >= 8 && typeof entry[entry.length - 2] === "number") return entry[entry.length - 2];
    if (entry.length === 5 && typeof entry[3] === "number") return entry[3];
    return -1;
  }

  function usesFlatColor(name) {
    return name === "hopper"
      || name === "torch" || name.endsWith("_torch")   // real texture applied via torchParts
      || name === "chest" || name === "trapped_chest" || name === "ender_chest" || name.endsWith("_chest");
  }

  function rotateCornerY(c, turns) {
    let x = c[0], z = c[2];
    for (let i = 0; i < turns; i++) [x, z] = [z, 1 - x];
    return [x, c[1], z];
  }

  function rotateBoxY(box, turns) {
    const [min, max] = box;
    const corners = [
      [min[0], min[1], min[2]], [min[0], min[1], max[2]], [min[0], max[1], min[2]], [min[0], max[1], max[2]],
      [max[0], min[1], min[2]], [max[0], min[1], max[2]], [max[0], max[1], min[2]], [max[0], max[1], max[2]],
    ].map(c => rotateCornerY(c, turns));
    const xs = corners.map(c => c[0]), ys = corners.map(c => c[1]), zs = corners.map(c => c[2]);
    return [[Math.min(...xs), Math.min(...ys), Math.min(...zs)], [Math.max(...xs), Math.max(...ys), Math.max(...zs)]];
  }

  function rotateShapeY(shape, turns) {
    turns = ((turns % 4) + 4) % 4;
    if (turns === 0) return shape;
    const rotateBoxPart = (part) => Array.isArray(part)
      ? rotateBoxY(part, turns)
      : { ...part, box: rotateBoxY(part.box, turns) };
    const rotatePlanePart = (part) => Array.isArray(part?.[0])
      ? part.map(c => rotateCornerY(c, turns))
      : { ...part, plane: part.plane.map(c => rotateCornerY(c, turns)) };
    return {
      opaque: shape.opaque,
      boxes: (shape.boxes || []).map(rotateBoxPart),
      planes: (shape.planes || []).map(rotatePlanePart),
    };
  }

  function facing4(offset, groupedByTwo = false, groupSize = 1) {
    if (offset < 0) return "north";
    const i = groupedByTwo ? Math.floor(offset / 2) % 4 : Math.floor(offset / groupSize) % 4;
    return ["north", "south", "west", "east"][i] || "north";
  }

  function turnsForFacing(facing) {
    return { north: 0, south: 2, west: 1, east: 3 }[facing] || 0;
  }

  function signTurns(offset) {
    if (offset < 0) return 0;
    const rot = Math.floor(offset / 2) % 16;
    return Math.round(rot / 4) % 4;
  }

  function wallTorchFacing(name, offset) {
    return facing4(offset, false, name.includes("redstone") ? 2 : 1);
  }

  function isWallTorch(name) {
    return name === "wall_torch" || name.endsWith("_wall_torch");
  }

  // Atlas tile for a torch's real texture (all wall/standing variants share it),
  // or -1 if textures aren't loaded (renderer falls back to a flat color).
  function torchTile(name) {
    if (atlasState !== "loaded") return -1;
    const stem = name.includes("soul") ? "soul_torch"
      : name.includes("redstone") ? "redstone_torch" : "torch";
    const t = atlasStems[stem];
    return typeof t === "number" ? t : -1;
  }

  // Crossed planes carrying the torch texture (or a flat color fallback).
  function torchParts(planes, name) {
    const t = torchTile(name);
    return planes.map(p => t >= 0
      ? { plane: p, tile: t, shade: 1 }
      : { plane: p, color: PART_COLORS.torchFlame, shade: 1 });
  }

  function stairShape(offset) {
    const facing = facing4(offset, false, 20);
    const halfTop = offset >= 0 && (Math.floor(offset / 10) % 2) === 0;
    const shapeIdx = offset >= 0 ? Math.floor(offset / 2) % 5 : 0;
    const shape = ["straight", "inner_left", "inner_right", "outer_left", "outer_right"][shapeIdx] || "straight";
    const slab = halfTop ? [[0, 0.5, 0], [1, 1, 1]] : [[0, 0, 0], [1, 0.5, 1]];
    const y0 = halfTop ? 0 : 0.5;
    const y1 = halfTop ? 0.5 : 1;
    let boxes;
    if (shape === "inner_left") {
      boxes = [slab, [[0, y0, 0.5], [1, y1, 1]], [[0, y0, 0], [0.5, y1, 0.5]]];
    } else if (shape === "inner_right") {
      boxes = [slab, [[0, y0, 0.5], [1, y1, 1]], [[0.5, y0, 0], [1, y1, 0.5]]];
    } else if (shape === "outer_left") {
      boxes = [slab, [[0, y0, 0.5], [0.5, y1, 1]]];
    } else if (shape === "outer_right") {
      boxes = [slab, [[0.5, y0, 0.5], [1, y1, 1]]];
    } else {
      boxes = [slab, [[0, y0, 0.5], [1, y1, 1]]];
    }
    return rotateShapeY({ opaque: false, boxes }, turnsForFacing(facing) + 2);
  }

  function shapeFor(name, stateOffset = -1) {
    if (!name) return { opaque: true, boxes: FULL_CUBE };
    // No `color` -> textured with the block's own tile (water_still/lava_still),
    // falling back to meta.color automatically when the atlas isn't loaded.
    if (name === "water") return { opaque: false, boxes: [{ box: [[0, 0, 0], [1, 0.875, 1]], shade: 1 }] };
    if (name === "lava") return { opaque: false, boxes: [{ box: [[0, 0, 0], [1, 0.875, 1]], shade: 1 }] };
    if (name.endsWith("_slab")) return { opaque: false, boxes: [[[0, 0, 0], [1, 0.5, 1]]] };
    if (name.endsWith("_stairs")) return stairShape(stateOffset);
    if (name.endsWith("_carpet")) return { opaque: false, boxes: [[[0, 0, 0], [1, 0.0625, 1]]] };
    if (name.endsWith("_bed")) return {
      opaque: false,
      boxes: [
        [[0, 0.1875, 0], [1, 0.5625, 1]],
        [[0.0625, 0, 0.0625], [0.1875, 0.1875, 0.1875]],
        [[0.8125, 0, 0.0625], [0.9375, 0.1875, 0.1875]],
        [[0.0625, 0, 0.8125], [0.1875, 0.1875, 0.9375]],
        [[0.8125, 0, 0.8125], [0.9375, 0.1875, 0.9375]],
      ],
    };
    if (name.endsWith("_pane") || name.endsWith("_bars")) return {
      opaque: false,
      boxes: [
        [[0.4375, 0, 0], [0.5625, 1, 1]],
        [[0, 0, 0.4375], [1, 1, 0.5625]],
      ],
    };
    if (name.endsWith("_fence") || name.endsWith("_wall")) return {
      opaque: false,
      boxes: [
        [[0.375, 0, 0.375], [0.625, 1, 0.625]],
        [[0.4375, 0.375, 0], [0.5625, 0.75, 1]],
        [[0, 0.375, 0.4375], [1, 0.75, 0.5625]],
      ],
    };
    if (name.endsWith("_door") || name.endsWith("_trapdoor")) return {
      opaque: false,
      boxes: [[[0, 0, 0], [1, name.endsWith("_trapdoor") ? 0.1875 : 1, 0.1875]]],
    };
    if (name === "ladder" || name === "vine" || name.endsWith("_wall_fan")) return {
      opaque: false,
      planes: [
        [[0, 0, 0.03125], [1, 0, 0.03125], [1, 1, 0.03125], [0, 1, 0.03125]],
        [[0.96875, 0, 0], [0.96875, 0, 1], [0.96875, 1, 1], [0.96875, 1, 0]],
      ],
    };
    if (name.endsWith("_rail") || name === "rail") return {
      opaque: false,
      planes: [
        [[0, 0.03125, 0], [1, 0.03125, 0], [1, 0.03125, 1], [0, 0.03125, 1]],
      ],
    };
    if (name.endsWith("_button")) return { opaque: false, boxes: [[[0.3125, 0.375, 0], [0.6875, 0.625, 0.125]]] };
    if (name.endsWith("_pressure_plate")) return { opaque: false, boxes: [[[0.0625, 0, 0.0625], [0.9375, 0.0625, 0.9375]]] };
    if (name.endsWith("_candle") || name === "candle") return {
      opaque: false,
      boxes: [
        [[0.375, 0, 0.375], [0.5625, 0.4375, 0.5625]],
        [[0.4375, 0.4375, 0.4375], [0.5, 0.625, 0.5]],
      ],
    };
    if (name === "torch" || (name.endsWith("_torch") && !isWallTorch(name)))
      return { opaque: false, planes: torchParts(TORCH_PLANES, name) };
    if (isWallTorch(name)) return rotateShapeY(
      { opaque: false, planes: torchParts(WALL_TORCH_PLANES, name) },
      turnsForFacing(wallTorchFacing(name, stateOffset)) + 2);
    if (name === "lantern" || name === "soul_lantern" || name.endsWith("_copper_lantern")) return {
      opaque: false,
      boxes: [
        [[0.375, 0, 0.375], [0.625, 0.125, 0.625]],
        [[0.3125, 0.125, 0.3125], [0.6875, 0.625, 0.6875]],
        [[0.375, 0.625, 0.375], [0.625, 0.875, 0.625]],
      ],
    };
    if (name === "chain" || name.endsWith("_chain")) return {
      opaque: false,
      boxes: [
        [[0.4375, 0, 0.4375], [0.5625, 1, 0.5625]],
        [[0.3125, 0.375, 0.4375], [0.6875, 0.625, 0.5625]],
      ],
    };
    if (name === "end_rod" || name.endsWith("_rod")) return {
      opaque: false,
      boxes: [
        [[0.4375, 0, 0.4375], [0.5625, 0.875, 0.5625]],
        [[0.3125, 0, 0.3125], [0.6875, 0.125, 0.6875]],
      ],
    };
    if (name === "chest" || name === "trapped_chest" || name === "ender_chest" || name.endsWith("_chest")) return {
      ...rotateShapeY({
        opaque: false,
        boxes: [
          [[0.0625, 0, 0.0625], [0.9375, 0.875, 0.9375]],
          { box: [[0.4375, 0.375, 0], [0.5625, 0.625, 0.0625]], color: PART_COLORS.chestLatch, shade: 1 },
        ],
      }, turnsForFacing(facing4(stateOffset, false, 6))),
    };
    if (name === "hopper") {
      const dirs = ["down", "north", "south", "west", "east"];
      const facing = dirs[stateOffset >= 0 ? stateOffset % 5 : 0] || "down";
      const sideSpout = facing === "down" ? [] : rotateShapeY({
        boxes: [{ box: [[0.375, 0.125, 0], [0.625, 0.375, 0.375]], color: PART_COLORS.hopperDark }],
      }, turnsForFacing(facing)).boxes;
      return {
        opaque: false,
        boxes: [
          { box: [[0.0625, 0.625, 0.0625], [0.9375, 1, 0.9375]], color: PART_COLORS.hopperLight },
          { box: [[0.1875, 0.375, 0.1875], [0.8125, 0.625, 0.8125]], color: PART_COLORS.hopperDark },
          { box: [[0.3125, 0.25, 0.3125], [0.6875, 0.375, 0.6875]], color: PART_COLORS.hopperDark },
          ...(facing === "down" ? [{ box: [[0.375, 0, 0.375], [0.625, 0.25, 0.625]], color: PART_COLORS.hopperDark }] : sideSpout),
        ],
      };
    }
    if (name === "cauldron" || name.endsWith("_cauldron")) return {
      opaque: false,
      boxes: [
        [[0.125, 0, 0.125], [0.875, 0.3125, 0.875]],
        [[0.0625, 0.3125, 0.0625], [0.1875, 1, 0.9375]],
        [[0.8125, 0.3125, 0.0625], [0.9375, 1, 0.9375]],
        [[0.1875, 0.3125, 0.0625], [0.8125, 1, 0.1875]],
        [[0.1875, 0.3125, 0.8125], [0.8125, 1, 0.9375]],
      ],
    };
    if (name === "campfire" || name === "soul_campfire") return {
      opaque: false,
      boxes: [
        [[0.125, 0, 0.25], [0.875, 0.1875, 0.375]],
        [[0.125, 0, 0.625], [0.875, 0.1875, 0.75]],
        [[0.25, 0.1875, 0.25], [0.75, 0.5, 0.75]],
      ],
    };
    if (name.endsWith("_wall_hanging_sign")) return {
      ...rotateShapeY({
        opaque: false,
        boxes: [
          [[0.125, 0.3125, 0], [0.875, 0.8125, 0.0625]],
          [[0.25, 0.8125, 0.015], [0.3125, 1, 0.0775]],
          [[0.6875, 0.8125, 0.015], [0.75, 1, 0.0775]],
          { box: [[0.2, 0.52, 0.065], [0.8, 0.57, 0.085]], color: PART_COLORS.signText, shade: 1 },
        ],
      }, turnsForFacing(facing4(stateOffset, true)) + 2),
    };
    if (name.endsWith("_wall_sign")) return {
      ...rotateShapeY({
        opaque: false,
        boxes: [
          [[0.0625, 0.25, 0], [0.9375, 0.75, 0.0625]],
          { box: [[0.2, 0.46, 0.065], [0.8, 0.51, 0.085]], color: PART_COLORS.signText, shade: 1 },
        ],
        // Base board sits on the -z face (faces +z); +2 turns seats it against
        // the support wall for the actual facing instead of the opposite face.
      }, turnsForFacing(facing4(stateOffset, true)) + 2),
    };
    if (name.endsWith("_sign") || name.endsWith("_hanging_sign")) return {
      ...rotateShapeY({
        opaque: false,
        boxes: [
          [[0.46875, 0, 0.46875], [0.53125, 0.5625, 0.53125]],
          [[0.0625, 0.375, 0.4375], [0.9375, 0.8125, 0.5625]],
          { box: [[0.2, 0.58, 0.565], [0.8, 0.63, 0.585]], color: PART_COLORS.signText, shade: 1 },
        ],
      }, signTurns(stateOffset)),
    };
    if (name === "flower_pot" || name.startsWith("potted_")) return {
      opaque: false,
      boxes: [
        [[0.3125, 0, 0.3125], [0.6875, 0.375, 0.6875]],
        [[0.25, 0.375, 0.25], [0.75, 0.5, 0.75]],
      ],
    };
    if (name === "cactus") return { opaque: false, boxes: [[[0.0625, 0, 0.0625], [0.9375, 1, 0.9375]]] };
    if (name === "cake") return { opaque: false, boxes: [[[0.0625, 0, 0.0625], [0.9375, 0.5, 0.9375]]] };
    if (name === "snow") return { opaque: false, boxes: [[[0, 0, 0], [1, 0.125, 1]]] };
    if (PLANT_NAMES.has(name) || name.endsWith("_sapling") || name.endsWith("_mushroom")) return {
      opaque: false,
      planes: CROSSED_PLANES,
    };
    return { opaque: true, boxes: FULL_CUBE };
  }

  function paletteMeta(entry, idx) {
    const name = entryName(entry);
    const stateOffset = entryStateOffset(entry);
    const shape = idx === 0 ? { opaque: false } : shapeFor(name, stateOffset);
    return {
      color: [entry[0] / 255, entry[1] / 255, entry[2] / 255],
      entry,
      name,
      stateOffset,
      opaque: shape.opaque === true,
      shape,
    };
  }

  function addVertex(data, wx, wy, wz, corner, color, uv, shade, tex) {
    data.push(
      wx + corner[0], wy + corner[1], wz + corner[2],
      color[0], color[1], color[2],
      uv[0], uv[1], shade, tex,
    );
  }

  function faceTileUV(entry, name, textured) {
    const tile = entryTile(entry, name, textured);
    return tile >= 0 ? tileUV(tile) : null;
  }

  function emitFace(data, wx, wy, wz, corners, entry, color, faceName, textured, shadeOverride = null, tileOverride = -1) {
    const uv = tileOverride >= 0 ? tileUV(tileOverride) : faceTileUV(entry, faceName, textured);
    const tex = uv ? 1 : 0;
    const shade = shadeOverride == null ? (FACE_SHADE[faceName] || 0.8) : shadeOverride;
    for (const k of TRI) {
      const t = uv ? uv[k] : [0, 0];
      addVertex(data, wx, wy, wz, corners[k], color, t, shade, tex);
    }
  }

  function emitBox(data, wx, wy, wz, min, max, entry, color, textured, occlude, shadeOverride = null, tileOverride = -1) {
    const boxFaces = [
      ["top", [[min[0], max[1], min[2]], [min[0], max[1], max[2]], [max[0], max[1], max[2]], [max[0], max[1], min[2]]], [0, 1, 0]],
      ["bottom", [[min[0], min[1], max[2]], [min[0], min[1], min[2]], [max[0], min[1], min[2]], [max[0], min[1], max[2]]], [0, -1, 0]],
      ["south", [[min[0], min[1], max[2]], [max[0], min[1], max[2]], [max[0], max[1], max[2]], [min[0], max[1], max[2]]], [0, 0, 1]],
      ["north", [[max[0], min[1], min[2]], [min[0], min[1], min[2]], [min[0], max[1], min[2]], [max[0], max[1], min[2]]], [0, 0, -1]],
      ["east", [[max[0], min[1], max[2]], [max[0], min[1], min[2]], [max[0], max[1], min[2]], [max[0], max[1], max[2]]], [1, 0, 0]],
      ["west", [[min[0], min[1], min[2]], [min[0], min[1], max[2]], [min[0], max[1], max[2]], [min[0], max[1], min[2]]], [-1, 0, 0]],
    ];
    for (const [faceName, corners, d] of boxFaces) {
      if (occlude && occlude(d)) continue;
      emitFace(data, wx, wy, wz, corners, entry, color, faceName, textured, shadeOverride, tileOverride);
    }
  }

  function emitPlane(data, wx, wy, wz, corners, entry, color, textured, shadeOverride = null, tileOverride = -1) {
    // CULL_FACE is disabled, so one quad is already visible from both sides.
    // (Emitting a reversed second copy would flip the UVs — upside-down on
    // vertically-asymmetric textures like the torch.)
    emitFace(data, wx, wy, wz, corners, entry, color, "south", textured, shadeOverride, tileOverride);
  }

  function partBox(part) {
    return Array.isArray(part) ? { box: part } : part;
  }

  function partPlane(part) {
    return Array.isArray(part?.[0]) ? { plane: part } : part;
  }

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
    const xray = tools.xray;
    // Under X-ray a hidden block reads as non-solid so its neighbours (ores,
    // structures) still draw the faces that touched it.
    const opaqueAt = (x, y, z) => {
      const idx = at(x, y, z);
      return idx !== 0 && metas[idx].opaque
        && !(xray && XRAY_HIDDEN.has(metas[idx].name));
    };
    const metas = pal.map(paletteMeta);

    const data = [];
    const hdata = [];
    for (let y = 0; y < ny; y++)
      for (let z = 0; z < nz; z++)
        for (let x = 0; x < nx; x++) {
          const idx = grid[(y * nz + z) * nx + x];
          if (idx === 0) continue;
          const meta = metas[idx], shape = meta.shape;
          const wx = ox + x, wy = oy + y, wz = oz + z;
          if (tools.chests && isHighlight(meta.name))
            emitHighlightCube(hdata, wx, wy, wz);
          if (xray && XRAY_HIDDEN.has(meta.name)) continue;  // see-through terrain
          if (shape.opaque) {
            for (const [name, corners, d] of FACES) {
              if (opaqueAt(x + d[0], y + d[1], z + d[2])) continue;
              emitFace(data, wx, wy, wz, corners, meta.entry, meta.color, name, textured);
            }
            continue;
          }
          for (const rawBox of shape.boxes || []) {
            const part = partBox(rawBox);
            const tile = typeof part.tile === "number" ? part.tile : -1;
            emitBox(data, wx, wy, wz, part.box[0], part.box[1], meta.entry,
              part.color || meta.color, part.color ? false : textured, null, part.shade, tile);
          }
          for (const rawPlane of shape.planes || []) {
            const part = partPlane(rawPlane);
            const tile = typeof part.tile === "number" ? part.tile : -1;
            emitPlane(data, wx, wy, wz, part.plane, meta.entry,
              part.color || meta.color, part.color ? false : textured, part.shade, tile);
          }
        }

    const arr = new Float32Array(data);
    gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
    gl.bufferData(gl.ARRAY_BUFFER, arr, gl.DYNAMIC_DRAW);
    vertexCount = arr.length / FLOATS;

    const harr = new Float32Array(hdata);
    gl.bindBuffer(gl.ARRAY_BUFFER, highlightVbo);
    gl.bufferData(gl.ARRAY_BUFFER, harr, gl.DYNAMIC_DRAW);
    highlightCount = harr.length / FLOATS;
  }

  // A solid gold cube (flat color, no texture) marking a container. Drawn later
  // with depth-testing off so it shows through walls.
  function emitHighlightCube(hdata, wx, wy, wz) {
    for (const [, corners] of FACES)
      for (const k of TRI)
        addVertex(hdata, wx, wy, wz, corners[k], HIGHLIGHT_COLOR, [0, 0], 1, 0);
  }

  function rebuildMesh() {
    if (lastPayload && atlasState !== "loading") buildMesh(lastPayload);
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
      lastPayload = payload;
      buildMesh(payload);
      if (!pose) {
        const initial = poseTarget || {
          eye: payload.eye.slice(), yaw: payload.yaw, pitch: payload.pitch,
        };
        pose = { eye: initial.eye.slice(), yaw: initial.yaw, pitch: initial.pitch };
        poseTarget = initial;
      }
      status(`${vertexCount / 6 | 0} faces` + (atlasState === "loaded" ? " · textured" : ""));
    } catch (e) {
      status("error");
    } finally {
      fetching = false;
    }
  }

  async function attachPrismarine(id, r) {
    botId = id;
    if (r) range = r;
    pose = null; poseTarget = null; vertexCount = 0;
    showPrismarineFrame();
    clearInterval(geomTimer); geomTimer = null;
    if (rafHandle) { cancelAnimationFrame(rafHandle); rafHandle = null; }
    if (gl) gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    status("loading Prismarine…");
    try {
      const res = await fetch("prismarine/index.html", { method: "HEAD", cache: "no-store" });
      if (!res.ok) {
        status("Prismarine renderer bundle not installed");
        return;
      }
      const frame = ensurePrismarineFrame();
      frame.src = `prismarine/index.html?v=20260714-instanced-cubes&bot=${encodeURIComponent(id)}&range=${encodeURIComponent(range)}&refresh=${encodeURIComponent(refreshMs)}`;
      status("Prismarine renderer");
    } catch {
      status("Prismarine renderer unavailable");
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
    const now = performance.now();
    const dt = lastFrameTime ? Math.min((now - lastFrameTime) / 1000, 0.1) : 0;
    lastFrameTime = now;
    if (tools.freecam && freecamPose) updateFreecam(dt);
    else smoothPose(dt);
    syncSize();  // keep the drawing buffer matched to the (possibly resized) canvas
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    const cam = tools.freecam && freecamPose
      ? freecamPose
      : (lookaroundPose && pose
        ? { eye: pose.eye, yaw: lookaroundPose.yaw, pitch: lookaroundPose.pitch }
        : pose);
    if (!cam || vertexCount === 0) return;

    // Freecam can roam far from the bot, so give it a much longer far plane.
    const far = (tools.freecam ? range * 3 + 32 : (range + 4) * 1.8);
    const proj = perspective(70 * Math.PI / 180, canvas.width / canvas.height, 0.1, far);
    const mvp = multiply(proj, viewMatrix(cam));

    gl.useProgram(prog);
    gl.uniformMatrix4fv(loc.uMVP, false, mvp);
    gl.uniform3fv(loc.uEye, cam.eye);
    gl.uniform1f(loc.uFogFar, range);
    if (atlasTex) { gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, atlasTex); }

    gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
    bindVertexAttrs();
    gl.drawArrays(gl.TRIANGLES, 0, vertexCount);

    // Container highlights: drawn last with depth-testing off (see through
    // walls) and fog pushed out of range so they read as solid gold markers.
    if (tools.chests && highlightCount) {
      gl.disable(gl.DEPTH_TEST);
      gl.uniform1f(loc.uFogFar, 1e9);
      gl.bindBuffer(gl.ARRAY_BUFFER, highlightVbo);
      bindVertexAttrs();
      gl.drawArrays(gl.TRIANGLES, 0, highlightCount);
      gl.enable(gl.DEPTH_TEST);
    }
  }
  function bindVertexAttrs() {
    setAttr(loc.aPos, 3, 0); setAttr(loc.aColor, 3, 12); setAttr(loc.aUV, 2, 24);
    setAttr(loc.aShade, 1, 32); setAttr(loc.aTex, 1, 36);
  }

  // Fly the detached camera from held keys (W/S along view, A/D strafe,
  // Space/Shift world up/down). Shift-less; look is driven by adjustLook.
  function updateFreecam(dt) {
    const fp = freecamPose;
    const f = forward(fp.yaw, fp.pitch);
    const r = normalize(cross(f, [0, 1, 0]));
    const k = (c) => freecamKeys.has(c) ? 1 : 0;
    const fwd = k("KeyW") - k("KeyS");
    const strafe = k("KeyD") - k("KeyA");
    const rise = k("Space") - (k("ShiftLeft") || k("ShiftRight") ? 1 : 0);
    const speed = 12 * dt;
    fp.eye[0] += (f[0] * fwd + r[0] * strafe) * speed;
    fp.eye[1] += (f[1] * fwd + rise) * speed;
    fp.eye[2] += (f[2] * fwd + r[2] * strafe) * speed;
    // Keep the bot camera advancing in the background so exiting freecam is smooth.
    smoothPose(dt);
  }
  function setAttr(l, size, off) {
    gl.enableVertexAttribArray(l);
    gl.vertexAttribPointer(l, size, gl.FLOAT, false, STRIDE, off);
  }

  function smoothPose(dt) {
    if (!pose || !poseTarget || dt <= 0) return;
    const distance = Math.hypot(
      poseTarget.eye[0] - pose.eye[0],
      poseTarget.eye[1] - pose.eye[1],
      poseTarget.eye[2] - pose.eye[2],
    );
    // Server teleports should be immediate; normal 20 Hz walking is smoothed.
    if (distance > 4) {
      pose.eye = poseTarget.eye.slice();
      pose.yaw = poseTarget.yaw;
      pose.pitch = poseTarget.pitch;
      return;
    }
    const alpha = 1 - Math.exp(-18 * dt);
    for (let i = 0; i < 3; i++)
      pose.eye[i] += (poseTarget.eye[i] - pose.eye[i]) * alpha;
    pose.yaw += angleDelta(pose.yaw, poseTarget.yaw) * alpha;
    pose.pitch += angleDelta(pose.pitch, poseTarget.pitch) * alpha;
  }

  function angleDelta(from, to) {
    return ((to - from + 540) % 360) - 180;
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
      if (rendererMode === "prismarine") {
        attachPrismarine(id, r);
        return;
      }
      if (!initGL()) return;
      showCustomCanvas();
      botId = id;
      if (r) range = r;
      pose = null; poseTarget = null; vertexCount = 0;
      lastFrameTime = 0;
      status("loading…");
      ensureAtlas(() => {
        if (botId !== id) return;
        refreshGeometry();
        clearInterval(geomTimer);
        geomTimer = setInterval(refreshGeometry, refreshMs);
      });
      if (!rafHandle) frame();
    },
    detach() {
      botId = null; pose = null; poseTarget = null; vertexCount = 0;
      lastPayload = null; highlightCount = 0;
      tools.freecam = false; freecamPose = null; freecamKeys.clear();
      lookaroundPose = null;
      lastFrameTime = 0;
      clearInterval(geomTimer); geomTimer = null;
      if (rafHandle) { cancelAnimationFrame(rafHandle); rafHandle = null; }
      if (gl) gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      if (prismarineFrame) {
        prismarineFrame.removeAttribute("src");
        prismarineFrame.hidden = true;
        prismarineFrame.style.display = "none";
      }
      if (canvas) {
        canvas.hidden = false;
        canvas.style.display = "block";
      }
      status("disabled");
    },
    setRange(r) {
      range = r;
      if (!botId) return;
      if (rendererMode === "prismarine") attachPrismarine(botId, range);
      else refreshGeometry();
    },
    setRefreshInterval(ms) {
      refreshMs = Math.max(1000, Number(ms) || DEFAULT_GEOM_REFRESH_MS);
      if (!botId) return;
      if (rendererMode === "prismarine") {
        attachPrismarine(botId, range);
        return;
      }
      clearInterval(geomTimer);
      geomTimer = setInterval(refreshGeometry, refreshMs);
      refreshGeometry();
    },
    resize() { syncSize(); },
    setPose(p) {
      if (!botId || p == null || p.x == null) return;
      poseTarget = {
        eye: [p.x, p.y + 1.62, p.z],
        yaw: controlActive ? poseTarget?.yaw ?? pose?.yaw ?? 0
          : p.yaw != null ? p.yaw : poseTarget?.yaw ?? pose?.yaw ?? 0,
        pitch: controlActive ? poseTarget?.pitch ?? pose?.pitch ?? 0
          : p.pitch != null ? p.pitch : poseTarget?.pitch ?? pose?.pitch ?? 0,
      };
    },
    adjustLook(dx, dy) {
      // In freecam, mouse-look steers the detached camera directly and never
      // touches the bot's pose (so it isn't reported to the server).
      if (tools.freecam && freecamPose) {
        freecamPose.yaw = (freecamPose.yaw + dx * 0.12 + 360) % 360;
        freecamPose.pitch = Math.max(-90, Math.min(90, freecamPose.pitch + dy * 0.12));
        return { yaw: freecamPose.yaw, pitch: freecamPose.pitch };
      }
      if (lookaroundPose) {
        lookaroundPose.yaw = (lookaroundPose.yaw + dx * 0.12 + 360) % 360;
        lookaroundPose.pitch = Math.max(
          -90, Math.min(90, lookaroundPose.pitch + dy * 0.12));
        return { yaw: lookaroundPose.yaw, pitch: lookaroundPose.pitch };
      }
      const current = poseTarget || pose;
      if (!botId || !current) return null;
      const yaw = (current.yaw + dx * 0.12 + 360) % 360;
      const pitch = Math.max(-90, Math.min(90, current.pitch + dy * 0.12));
      poseTarget = { eye: current.eye.slice(), yaw, pitch };
      if (pose) {
        pose.yaw = yaw;
        pose.pitch = pitch;
      }
      return { yaw, pitch };
    },
    look() {
      const current = poseTarget || pose;
      return current ? { yaw: current.yaw, pitch: current.pitch } : null;
    },
    setLookaround(active) {
      if (active) {
        const current = pose || poseTarget;
        lookaroundPose = current
          ? { yaw: current.yaw, pitch: current.pitch }
          : { yaw: 0, pitch: 0 };
      } else {
        lookaroundPose = null;
      }
    },
    isLookaround() { return lookaroundPose !== null; },
    setControlActive(active) { controlActive = !!active; },
    // Enable/disable a tool. "freecam" detaches the camera; "xray"/"chests"
    // change the mesh and need a rebuild.
    setTool(name, on) {
      on = !!on;
      if (name === "freecam") {
        tools.freecam = on;
        freecamKeys.clear();
        if (on) {
          const src = pose || poseTarget;
          freecamPose = src
            ? { eye: src.eye.slice(), yaw: src.yaw, pitch: src.pitch }
            : { eye: [0, 80, 0], yaw: 0, pitch: 0 };
        } else {
          freecamPose = null;
        }
      } else if (name === "xray") {
        tools.xray = on;
        rebuildMesh();
      } else if (name === "chests") {
        tools.chests = on;
        rebuildMesh();
      }
    },
    getTools() { return { ...tools }; },
    setFreecamKey(code, down) {
      if (down) freecamKeys.add(code); else freecamKeys.delete(code);
    },
    isFreecam() { return tools.freecam; },
    isOn() { return botId !== null; },
    renderer() { return rendererMode; },
    setRenderer(mode) {
      rendererMode = mode === "prismarine" ? "prismarine" : "custom";
      localStorage.setItem("visionRenderer", rendererMode);
      if (botId) {
        const id = botId, r = range;
        this.detach();
        this.attach(id, r);
      }
    },
    element() {
      if (rendererMode === "prismarine") return ensurePrismarineFrame();
      if (!canvas) initGL();
      return activeElement();
    },
  };
})();
