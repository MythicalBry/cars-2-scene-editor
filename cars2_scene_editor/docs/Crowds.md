# Grandstand crowds and the game's Lua data tables

The crowds of cars in the grandstands of italy, london_a/b and tokyo_a/b/c are **not choreographies**. The
world logic declares them as `CrowdMemberPlane` nodes, and the engine's `CrowdManager` lays them out and draws
them as animated sprites. The car types come from a compiled Lua table. The Cars 2 Scene Editor implements
this in `crowd.py` (layout, drawing, editing, export) and `core/luatable.py` (the Lua reader).

The `crowd_a`/`crowd_b`/`crowd_c` choreography packages (3D crowd cars) are a different thing: they are not
used by these planes.

---

## 1. The logic side

A world has a merged crowd namespace, e.g. `TKO_B_Crowd` in tokyo_b, with one `CrowdMemberPlane` per stand
section:

```
CrowdMemberPlane  Transform  -> TransformFetch  <- LOC_Crowd_TKO_091 (a locator)
                  Width      -> FloatFetch  Value 30.0   "Row Width"
                  RowSpacing -> FloatFetch  Value 5.0    "Row Spacing"
                  NumRows    -> IntFetch    Value 1      "Number of Rows"
                  NumColumns -> IntFetch    Value 7      "Crowd per Row"
```

The values sit on the port nodes. Editing them (`Value` of the four port nodes) changes the crowd; the editor
writes them back by the plane's UUID.

| World | Planes | Members |
|---|---|---|
| italy | 12 | 678 |
| london_a | 11 | 286 |
| london_b | 8 | 208 |
| tokyo_a | 104 | 854 |
| tokyo_b | 42 | 342 |
| tokyo_c | 101 | 809 |

## 2. Layout (`FUN_005a3800`, the plane node's start-up)

```c
// FUN_005a3800 (decompiled, trimmed and renamed)
xf    = fetch(Transform);                 // position p, rotation q
W     = fetch(Width)       or 1;   S = fetch(RowSpacing) or 1;
rows  = fetch(NumRows)     or 1;   cols = fetch(NumColumns) or 1;
side  = rotate(q, (-1, 0, 0));            // along the row
back  = rotate(q, ( 0, 0, -1));           // row to row
gap   = cols > 1 ? W / (cols - 1) : 0;
ntypes = CrowdManager->typeCount;         // 4
for (r = 0; r < rows; r++) {
    base  = p + back * (r * S);
    half  = W * 0.5;  n = cols;
    if (r & 1) { n = cols - 1; half -= gap * 0.5; }      // odd rows: one fewer, staggered
    start = base - side * half;
    for (c = 0; c < n; c++) {
        at = start + side * (c * gap);
        if (run == ntypes) {                              // every <ntypes> members:
            run = 0; step = (step + 1) % 5;               //   next stepping prime,
            kind = random() % ntypes;                     //   random start
        }
        run++;
        kind = (kind + STEPS[step]) % ntypes;             // STEPS (0x018556f8) = 631 641 643 647 653
        foot = type[kind].geometryWidth * type[kind].facingPercent;     // FUN_00534350
        if (foot < gap)                                   // narrower than its gap: jitter sideways
            at += side * random_range(-(gap - foot) / 2, (gap - foot) / 2);
        CrowdManager_AddMember(kind, at);                 // FUN_00532c40
    }
}
```

`FUN_00532c40` raises each member by half its `geometryHeight`, so the sprite stands on the plane.
`FUN_00534680` stores the member with a random phase, `UpTo(1.0)`, for its bounce.

## 3. The types: `crowdmanagerinfo.lua`

`CrowdManager`'s constructor (`FUN_00531d50`) loads `LuaTables/CrowdManagerInfo.lua`. It is in
`assets/luatables/luatables.zip` as compiled Lua 5.1 bytecode. Decoded:

```lua
CrowdManagerInfo = {
  MaterialInfoList = { { name = "CrowdMember2", width = 2048, height = 1024 } },
  CrowdMemberList = {
    { name = "RedCar",    textureName = "CrowdMember2", numFrames = 16, frameWidth = 256, frameHeight = 128,
      startColumn = 0, startRow = 0, geometryWidth = 7, geometryHeight = 3.5,
      endDeadZone = 30, facingPercent = 0.6, isMirrored = true },
    { name = "BlueCar",   ... startRow = 2, geometryWidth = 7, geometryHeight = 3.5 ... },
    { name = "GreenVan",  ... startRow = 4, geometryWidth = 8, geometryHeight = 4   ... },
    { name = "YellowCar", ... startRow = 6, geometryWidth = 7, geometryHeight = 3.5 ... },
  },
  BounceInfo = { { bounceHeight = 0.1, bounceSpeed = 2, avatarExcitementRange = 20,
                   excitedHeightFactor = 1.5, excitedSpeedFactor = 2 } },
}
```

- The atlas is the world texture `crowdanim0_60new16fr` (2048 × 1024, DXT5, sRGB). Each type has 16
  pre-rendered frames of 256 × 128, eight per atlas row and two rows per type, turning from facing the viewer
  (frame 0) towards side-on, about 0–60° (90° − `endDeadZone`). The other side is the same frames mirrored.
- "YellowCar" is a pale-blue mini in the atlas.
- `endDeadZone` is stored as `cos(endDeadZone°)`.
- `avatarExcitementRange` and the `excited*` factors make cars bounce harder when a racer is within 20 m (not
  reproduced by the editor).

How the editor draws a member: an upright sprite turned to the camera, `geometryWidth × geometryHeight`. The
frame is picked from the angle between the member's facing (towards the track, `−back`) and the direction to
the camera on the ground plane, and mirrored when the camera is on the other side. The member bounces
`bounceHeight × |sin(π (bounceSpeed t + phase))|`. The exact frame pick of the game's draw callback
(`CrowdManager` drawing, `0x00532ea0` → `FUN_00604360`) was not traced.

## 4. Reading the game's compiled Lua tables

`luatables.zip` holds the game's data tables as **Lua 5.1 bytecode, big-endian** (header
`1B 4C 75 61 51 00 00 04 04 04 08 00`). They only build constant tables and assign them to globals, so a small
reader that replays the main function's instructions is enough; no Lua VM is needed:

```python
# core/luatable.py (abridged): replay LOADK / NEWTABLE / SETTABLE / SETLIST / SETGLOBAL
for ins in code:
    op, a = ins & 0x3F, (ins >> 6) & 0xFF
    c, b, bx = (ins >> 14) & 0x1FF, (ins >> 23) & 0x1FF, ins >> 14
    if   op == LOADK:     reg[a] = k[bx]
    elif op == NEWTABLE:  reg[a] = {}
    elif op == SETTABLE:  reg[a][rk(b)] = rk(c)          # rk(x): constant when x >= 256
    elif op == SETLIST:   for i in 1..b: reg[a][(c - 1) * 50 + i] = reg[a + i]
    elif op == SETGLOBAL: globals[k[bx]] = reg[a]
```

The function layout is standard Lua 5.1: source, line info, upvalue, parameter and vararg counts, max stack,
code, constants (nil, boolean, number, string), nested functions, then debug info. `core/luatable.py` handles
either byte order.
