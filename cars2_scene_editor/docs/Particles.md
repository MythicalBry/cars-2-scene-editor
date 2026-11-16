# Cars 2 Arcade particles: the Pigpen data files and how choreographies start them

Covers the particle library format (`assets/particles/*.zip`), what each record field means, and how a
choreography's logic starts particle effects. Findings come from `sdaemon.exe` (Cars 2 Arcade, the Ghidra
project `/Cars-2-Arcade`) and were checked against all 80 shipped particle zips. The Cars 2 Scene Editor
implements them in `core/pigpen.py` (the reader) and `particles.py` (the preview simulation).

Excerpts marked *decompiled* are Ghidra output, trimmed and renamed for reading. Addresses are for the Arcade
`sdaemon.exe`.

---

## 1. Where particles live

| File | What it holds |
|---|---|
| `assets/particles/<world>_d1.zip` … `_d4.zip` | One library per world and detail level (d1 = highest) |
| `assets/particles/universal.zip` | Definitions every world shares |
| `<zip>/<stem>.dbl` | The Pigpen data file: emitters, effects, particle systems (binary, no field names) |
| `<zip>/<stem>.oct` | `MaterialPool` (the particle materials) and `TexturePool` (their textures) |
| `<zip>/<stem>_<n>.tbuf` | Texture data: plain DDS files |
| `<zip>/effects/part_*.tfx` | The particle shaders the materials use |

This `.dbl` is **not** the GBWIN `.dbl` that worlds use for splines. It is read by `FUN_00d6b410` and `dblLoad` (`0x00752cb0`).

## 2. The Pigpen `.dbl` container

```
0x00  "0"            atoi'd offset (always 0)
0x08  "DX"
0x10  u32 0x100      version
0x20  chunks...
```

Each chunk:

```
u16 type
u16 flags
u32 size            (data size, after the header)
header is 10 bytes, or 0x20 when flags & 8; plus a 0x20-byte name when flags & 2 (e.g. "1000")
data[size]
```

Each chunk's data starts with a **table**: `u16 count, u16 flags`.

- `flags & 3 == 1`: `count` u32 offsets follow, each relative to the table start.
- `flags & 3 == 2`: a u32 stride follows; fixed-size records start at +8 (+0x10 when `flags & 8`).

| Chunk type | Records | Names chunk | Loader slot (`this+…`) | Fix-up function |
|---|---|---|---|---|
| `0x64` | emitters (264 bytes, fixed) | `0x84` | `0x5b0` | `FUN_00756090` |
| `0x65` | particle systems (508 bytes) | `0x88` | `0x560` | `FUN_00755e60` → `FUN_00755d20` per record |
| `0x66` | effects (variable size) | `0x85` | `0x588` | `FUN_00755c70` |
| `0x6a`, `0x6c`, `0x6d`, `0x6e` | (trail and ribbon data; not decoded) | `0x8a`–`0x8d` | `0x5d8` … `0x650` | `FUN_00755ef0`, `…f80`, `…fc0`, `FUN_00756050` |

The name chunks hold one null-terminated string per record, in record order; the loader writes each name
pointer into the record's first word. Reading a library:

```python
def chunks(d):
    assert struct.unpack_from('<I', d, 0x10)[0] == 0x100
    o, out = 0x20, []
    while o + 8 <= len(d):
        typ, flags, size = struct.unpack_from('<HHI', d, o)
        hsz = (0x20 if flags & 8 else 10) + (0x20 if flags & 2 else 0)
        out.append((typ, flags, o + hsz, size))
        o += hsz + size
    return out
```

## 3. Particle system record (chunk `0x65`, 508 bytes)

Named from the spawn function `FUN_00d90fc0` (one particle from a system), the velocity function
`FUN_00d93fd0` and the system fix-up `FUN_00755d20`.

| Offset | Type | Meaning | Decoding |
|---|---|---|---|
| +0x00 | u32 | name pointer (filled by the loader) | |
| +0x08 | u32 | flags | `0x20` one size for both axes; `0x40` random start rotation; `0x80` random flipbook start frame; `0x4000000` colour from the RGB curves; `0x1` align to velocity; `0x200` (see `0x400000`) |
| +0x0c | u16 | material | index into the library `.oct` MaterialPool |
| +0x90 | u8 | flipbook rate | frames per second (`1 / rand(v)` seconds per frame) |
| +0x92 | u8 | lifetime | `v / 255 × 50` seconds; 0 = lives on |
| +0x94 | 3 × s8 | emit direction | `v / 127` (used when the caller gives none) |
| +0x98 | u16 | half width | `v / 65535 × 1000 × 0.5` metres |
| +0x9a | u16 | half height | as above (ignored with flags `0x20`) |
| +0x9e | u8 | which curve scales the height | curve index |
| +0x9f | u8 | flags 2 | `0x10` no velocity |
| +0xa2 | u16 | max distance | `(v / 65535 × 500)²` |
| +0xb8 | f32 | speed | metres per second |
| +0xbd, +0xbe | u8 | cone angle, random extra | `v / 255 × π` |
| +0xc0 | 3 × f32 | spin | radians per second (randomised when +0xcc ≠ 0) |
| +0xfc | 6 × curve | curves (0x24 bytes each, see below) | 0 size, 1 height, 2 R, 3 G, 4 B, 5 alpha |
| +0x1d4 | 3 × f32 | start colour | 0–255 |
| +0x1e0 | 3 × f32 | end colour | 0–255 |
| +0x1ec | u16 | sub-emitter | emitter index, `0xffff` = none |

**A curve** is 8 floats and a type byte at +0x20. Type 3 is a constant (`f[0]`). The others read as a cubic
Bezier from `(0, f0)` through `(f1, f2)` and `(f3, f4)` to `(1, f5)`; `f6` and `f7` are usually 1 and 0. The
loader triples `f2` and `f4` of type-0 curves, which suggests type 0 stores tangents:

```c
// FUN_00755d20 (decompiled): fix-up per system record
for (k = 0; k < 6; k++) {
    float *c = rec + 0xfc + k * 0x24;
    if (*(char *)(c + 0x20) == 0) { c[2] *= 3.0f; c[4] *= 3.0f; }
}
```

How the spawn function reads the size and lifetime:

```c
// FUN_00d90fc0 (decompiled, trimmed): one particle of system `sys`
p->life     = rand_pct(sys[0x92] / 255.0f * 50.0f);
p->inv_life = p->life == 0 ? 100000.0f : 1.0f / p->life;
p->halfW    = rand_pct(*(u16 *)(sys + 0x98) / 65535.0f * 1000.0f) * scale * 0.5f;
if (curve(sys + 0xfc).type == 3 || p->life == 0)        // constant size curve
    p->size = p->halfW * curve(sys + 0xfc).f[0];
if (sys_flags & 0x4000000) { /* colour from curves 2..4 */ }
else                       { /* FUN_00d90ef0: start colour -> end colour */ }
p->velocity = FUN_00d93fd0(...) * speed_scale;          // direction (+0x94), speed (+0xb8), cone (+0xbd)
```

`FUN_00d8d140(value, pct)` randomises a value by a percentage. The preview leaves it out, so bursts land
in the right place at the right time but not on the same pixels as the game.

Example, tokyo_b's `FrwkBurstRedAPrtcl` (system 40 of `tokyo_b_d1`): material 14 (`BasicRedSparkA`, texture
`fireworkbrsta`), lifetime 0.39 s, half size 75 m, drifting down at 0.6 m/s, red → yellow, size curve rising
from 0 to 1 and alpha fading from 1.5 to 0.

## 4. Effect record (chunk `0x66`)

An effect starts several systems together.

```
+0  u32 name pointer
+4  u32 entry count
+8  entries, 0x14 bytes each:
    +0x00 u32 system index
    +0x04 f32 delay        (randomised by byte +0x10 percent)
    +0x08 f32 spread       (randomised by byte +0x0f percent; the time the entry's particles are spread over)
    +0x0c u8  count
    +0x0e u8  count variance  (count = base ± variance, at least 1)
    +0x11 u8  chance percent  (0x64 = always)
```

```c
// FUN_00d60480 (decompiled, trimmed): starting an effect
for (i = 0; i < eff->count; i++, e += 0x14) {
    int lo = e[0xc] - e[0xe];
    n[i]     = max(1, lo + random_below(e[0xc] + e[0xe] - lo));
    spread[i] = rand_pct(*(float *)(e + 8), e[0xf]);
    delay[i]  = rand_pct(*(float *)(e + 4), e[0x10]);
}
```

Example, `FrwrksA`: five entries. Red, gold, green and blue bursts with chances of 40, 40, 20 and 20 %, and
`FrwkPopFlashBprtcl` with count 5 ± 5 spread over 0.283 s.

## 5. Emitter record (chunk `0x64`, 264 bytes)

An emitter keeps starting an effect.

| Offset | Meaning |
|---|---|
| +0x04 u16 | effect index |
| +0x06 u8 | kind (0, 1, 6: point; 5: other; 2–4 rejected) |
| +0x07 u8 | rate: `v / 255 × 100` effects per second |
| +0x08 u8 | rate variance |
| +0x09 u8 | flags (`1` fixed rate, `2` fixed timer, `8` life from +0x21) |
| +0x10 u16 | radius: `v / 65535 × 1000` metres |
| +0x1c u16 | timer: `v / 65535 × 100` |
| +0x21 u8 | life: `v / 255 × 50` seconds (0 = while on) |
| +0x30 | six curves, as in systems |

From `FUN_00d60e40` (emitter start). `EmitFrwrksA`: effect 26 (`FrwrksA`), 0.78 per second, radius 5 m.

## 6. Materials and textures

A system's material index points into the library `.oct` MaterialPool. Its `Base` texture entry points into
the TexturePool, whose `FileName` is a `.tbuf` (a DDS file). The material's effect file
(`effects/part_*.tfx` in the zip) gives the blend:

| Effect name part | Pass states | Draws as |
|---|---|---|
| `…_ar2b_r`, `…_ar2eb_r` | `AlphaBlendEnable 1`, `SrcBlend One`, `DestBlend InvSrcAlpha`, `ZEnable 0`, `ZWriteEnable 0` | premultiplied alpha, no depth |
| `…_ak_r` | `AlphaTestEnable 1`, `AlphaBlendEnable 0`, `ZWriteEnable 1` | alpha-tested cut-out |

The particle `.tfx` files have a `Shader` technique only, not `Standard`.

## 7. How a choreography starts particles

A choreography package's own logic file (`<package>.mer`) holds the particle nodes:

| Node | Fields | Starts it | Where |
|---|---|---|---|
| `ParticleEffect` | `effect_name`, `use_orientation`, `SurfaceTypeCheck` | an **Impulse** link | the locator its `TransformFetch` link names |
| `ParticleEmitter` | `emitter_name`, `use_orientation` | a **Surge** link: on while the surge is on | as above |

The names resolve through the world's library (`<world>_d1`, then `universal`).

The impulses and surges come from **tickers** in the package network:

- `TickerPool` entries: `{NodeName: 'ANC_fireWorksA_00|tickerImpulse_PrtcleBurstRed', Type: 'Impulse'}` or
  `Type: 'Surge'`.
- In the `.mer`, a ticker is a `NodeGroup` named after it, whose `Event` (`<ticker> / Out`) or `EventSurge`
  feeds the particle node.
- **Impulse tickers** fire at the times of an `EventList` data node,
  `<anchor>/<ticker>/InputImpulse/EventList_EventListData`. Its `NetworkBufferOffset` points into the
  SubNetwork's `NetworkBuffer`:

  ```
  f32 start, f32 end, u16 ?, u16 flags (0x0600), u32 count, then count × (f32 time, f32 value)
  ```

- **Surge tickers** are on while the clip keys `<anchor>/<ticker>/InputSurge` (a `Bool` data node) on.

tokyo_b's `fireWorksA` (3 s, looping):

| Ticker | Fires at | Effect | Locator |
|---|---|---|---|
| `tickerImpulse_PrtcleBurstRed` | 0, 1.33 s | `FrwkBurstRedA` | `LocPrtcl_BurstRed` |
| `tickerImpulse1_PrtcleBurstGold` | 0.33, 1.67 s | `FrwkBurstGoldA` | `LocPrtcl_BurstGold` |
| `tickerImpulse2_PrtcleBurstGreen` | 0.67, 2.0 s | `FrwkBurstGreenA` | `LocPrtcl_BurstGreen` |
| `tickerImpulse3_PrtcleBurstBlue` | 1.0, 2.33 s | `FrwkBurstBlueA` | `LocPrtcl_BurstBlue` |
| `tickerSurge_PrtcleEmitFrwrksA` | while on | emitter `EmitFrwrksA` | `LocPrtcl_EmitFrwrksA` (209 m up) |

The same package has `SoundEffect` nodes (`firework_explode`) on the same impulses. Its `Light*` meshes are
ambient light volumes; see [AmbientLightVolumes.md](AmbientLightVolumes.md).

## 8. Runtime entry points (for further work)

| Function | Address |
|---|---|
| create an effect | `0x00d8ce70` → name lookup `FUN_00d6aae0` → `FUN_00d8cae0` → `FUN_00d60480` |
| create an emitter | `0x00d8c9c0` → `FUN_00d8ca30` → `FUN_00d60e40` |
| emitter lookup | `0x00d8bba0` |
| spawn one particle | `FUN_00d90fc0` (position `FUN_00d930f0`, velocity `FUN_00d93fd0`) |
| spawn one trail or ribbon | `FUN_00d58bb0` (via `FUN_00d5fdf0`; uses another record type) |

## 9. Open questions

- What the trail and ribbon chunks (`0x6a`–`0x6e`) hold.
- The exact curve evaluation (the Bezier reading is inferred from the data and the fix-up).
- The randomisation stream (`FUN_00d8d140`).
- A world's own emitters (Tokyo rain, steam, sparks), which the world logic starts rather than a
  choreography.
