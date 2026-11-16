# SubNetworks in worlds and choreographies

A **SubNetwork** is Octane's dataflow program: data nodes hold values, and processor nodes compute new values
from them every frame. The format is the same everywhere it appears. The Cars 2 Toolkit's
[SubNetworkPool.md](../../../cars2-character-toolkit/cars2_toolkit/docs/SubNetworkPool.md) explains it in full on
a character rig: headers, every field, evaluation order, why editing is safe. This page covers how **worlds**
and **choreography packages** use it, and what the Scene Editor does with it.

| Where | Network | Role |
|---|---|---|
| world `.oct` | one `Type 'Scene'` network | named placements Meridian reads and moves; texture animation; spline followers; timed visibility |
| choreography `.oct` | one network per package | animates the package's scene: cameras, props, lights, characters through `LayerBind`s |
| character `.oct` | the rig | see the toolkit's page |
| clip `.oct` | `Type 'Clip'` | one ClipData node plus LayerBinds |

## The world network

Surveyed on all 21 worlds in `vanilla_worlds/allw`:

| | All worlds |
|---|---|
| Data nodes | `Dilation` 15,777, `Transform` 1,034, `Float` 310, `ClipData` 58, `Bool` 22 |
| Processors | `ClipProcessor` 58, `FrameSelector` 40 |

Per world it ranges from 71 data nodes (tutorialadvanced) to 1,828 (italy). Race worlds use FrameSelectors;
mission worlds (`mi_*`) use ClipProcessors.

A world's network has only `Name`, `Type`, the header tables, the two pools and `NetworkBuffer`. It has no
connection maps, rest positions or template links: nothing in a world is animated by character-style clips.

### 1. Named placements: almost every node

On tokyo_b, 1,116 of the 1,119 data nodes are `<NodeName>/Transform` (a Dilation, or a Transform when it has a
rotation or scale). **Every one is named after a scene-tree node**, and 1,091 of them are bound by a `Transform`
association. Example:

```
DriveTo_01/Transform   Dilation   Translation [-958.4, 27.7, 102.7]  Orientation#Rotor [0, -0.992, 0, 0.123]
```

This is how **Meridian sees the world**: a LocatorNode or a choreography anchor fetches a placement by name
through these nodes, not from the scene tree directly. So the exporter's `sync_subnetwork` does two things:

- **moved nodes**: writes the new translation, rotation (sign kept continuous with the old rotor) and scale
  into the node's `<Name>/Transform` data. Otherwise the logic would still see the old position;
- **new locators**: adds `<Name>` and `<Name>/Transform` paths, re-allocates the header tree
  (`c2headers.allocate` / `encode_paths`), remaps every existing node's `Header`, and appends a data node
  (Dilation, or Transform when rotated or scaled). This goes together with the new `SceneNodeRefsPool` entry,
  `Transform` association and Meridian LocatorNode ([ImportExport.md](ImportExport.md)).

Data nodes are only appended, so no processor reference changes.

### 2. Texture animation: `FrameSelector`

```
Global.Time (Float) --> FrameSelector 'MaterialComponent0_FrameCurve' --> MaterialComponent0_FrameNumber (Float)
MaterialComponentPool#0: Type TextureAnimation, Usage diffuseMapA1, TextureReferences [216 .. 221],
                         AnimationNode 'MaterialComponent0_FrameNumber'
```

`Global.Time` is the engine's clock (a Float data node every network can read). A FrameSelector turns it into a
frame number using a curve in the `NetworkBuffer`. The material component swaps its slot's texture by that
number, and the association `MaterialComponentRef_MaterialLink` says which material. The Game renderer
reproduces these flipbooks (`dynamic` commands in `gamerender`), and the editor leaves the network part as
shipped.

### 3. Spline followers and timed visibility: `ClipProcessor`

The same driven-key ClipProcessor characters use, driven by a world Float:

```
jetSpline_Locator/timeInput --ClipProcessor--> jetSpline_Locator/MotionPathU     (mi_air: a jet along a spline)
Global.Time                 --ClipProcessor--> Seperator/Visibility              (italy)
```

Mission logic sets `timeInput`, and the curve maps it to a position along a motion path (`MotionPathU`). The
README's "spline followers" are these. `Bool` nodes such as `ACT_baggageDriver22/Visibility` are visibility
switches the logic flips.

### What the editor edits, and what it doesn't

- **Edited:** `<Name>/Transform` values of moved nodes, and new locator transforms.
- **Passed through:** FrameSelectors, ClipProcessors and their ClipData, Bool switches, the network buffer.
- **Never:** a renamed node gets no new network node, and its old one is not renamed. A renamed locator
  therefore loses its Meridian binding, which is one reason `export_name` keeps imported names.

## Choreography networks (`core/choreo.py`)

A choreography package (`assets/choreographies/<name>/` or `<name>.zip`) is a small scene: an anchor Transform
(`ANC_<name>_00`) with geometry, bones, cameras and lights under it, plus a SubNetwork that animates them. These
are the pieces `choreo.py` evaluates, measured on all 538 shipped packages:

| Piece | Behaviour |
|---|---|
| Data nodes | paths mirror the scene (`ANC_X_00/SKIN/smoke03/Transform`); a scene node binds to its data node through an association with the same name, `|` separated |
| Transform / Dilation | `Translation`, `Orientation#Rotor` (stored conjugated) or `#OrientProxy` (ParentProxyToParent × LocalProxyToParentProxy × LocalToLocalProxy, all conjugated), `Scale`; local = T·R·S; `BasisDataNodeRef` = parent |
| `ClipProcessor` | `Global.Time` → ClipDataBlock → outputs. **Values are absolute here**: location replaces Translation, Euler replaces the rotation, scale replaces Scale; scalars and Bools replace the value |
| `AnimCurve3ToPosition`, `AnimCurve3Euler(ZXY|XYZ)ToRotor` | three Maya-style Hermite curves in the NetworkBuffer drive `<X>/Transform.Translation` / `.Orientation` |
| `Connection` | copies input to output |
| `OffsetKinematicStateUpdate` | places a referenced character at its `<Namespace>:Anchor/Transform` relative to the anchor |
| `LayerBind` | plays a clip **into a referenced character** (`Luigi:Choreography`, template `car_choreography`); slot s → the character's `ConnectionMapUsedDataNodes[DataNodeLinks[s]]`. These values are **offsets from the rig's rest** (character clip convention) |

Not evaluated by the editor: UberConstraint, RadialBasis, Relationship, DynamicChain. For characters, the
preview hands the rig to the Cars 2 Toolkit, which rebuilds those ([Previews.md](Previews.md)).

Note the two conventions: **choreography clips on scene nodes are absolute; clips into a character are offsets
from rest.** `clipeval` evaluates both kinds of block; the caller decides which applies.

## Working with a world network from Python

```python
import sys; sys.path.insert(0, 'cars2_scene_editor/core')
import c2oct
from c2headers import decode_paths
t = c2oct.load('tokyo_b.oct')
sn = t['SubNetworkPool']['SubNetwork#0']
paths = decode_paths(sn['HeaderStrings'], sn['HeaderStringIndices'], sn['HeaderLayout'])
for d in sn['DataNodePool'].values():
    if d['Type'] == 'Float':
        print(paths[d['Header'] - 1], d.get('Data'))     # Global.Time, MaterialComponent0_FrameNumber ...
```
