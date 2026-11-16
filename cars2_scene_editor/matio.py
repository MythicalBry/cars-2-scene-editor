"""Game materials and textures <-> Blender: shared by the Cars 2 Scene Editor
(worlds) and the Cars 2 Toolkit (characters), which carries a copy of this
material system.

    fill_texture_table   TexturePool -> a C2Texture list (+ the extracted .dds files)
    fill_material_props  MaterialPool entry -> mat.c2
    write_props          mat.c2 -> MaterialPool entry (only what changed or was added;
                         slots / parameters a shader switch dropped are left out)
    add_new_textures     textures added in Blender -> new TexturePool entries
    apply_replacements   replaced files / changed sRGB flags -> their TexturePool entries
"""
import os

import bpy
import numpy as np

import dds
from c2oct import FloatList


def fill_texture_table(table, textures, folder):
    """Write every TexturePool texture (`textures`, the pool's entries in order)
    to <folder>/<its source path>.dds (once), list them in `table` (a C2Texture
    collection: a world's textures or a character's Object.c2tex) and load the
    2D ones. -> {texture index: bpy image}.
    """
    images = {}
    table.clear()
    for i, tx in enumerate(textures):
        data = tx.get('Data', b'')
        src = tx.get('SourceFilePath', tx.get('Name', 'tex%d' % i)).replace('\\', '/')
        rel = os.path.splitext(src)[0] + ('.dds' if data[:4] == b'DDS ' else os.path.splitext(src)[1])
        path = os.path.join(folder, rel)
        if data and not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(data)
        item = table.add()
        item.name = tx.get('Name', 'tex%d' % i)
        item.index = i
        item.source_path = tx.get('SourceFilePath', '')
        item.ttype = tx.get('Type', '2d')
        item.file = path
        item.srgb = dds.is_srgb(data)
        item.info = info_text(dds.info(data))
        if data and tx.get('Type', '2d') == '2d':
            try:
                images[i] = bpy.data.images.load(path, check_existing=True)
            except RuntimeError:
                pass
    return images


def info_text(inf):
    """dds.info() -> '1024x1024 DXT1, 11 mips' (the texture lists' size / format line), '' for None."""
    return ('%dx%d %s%s, %d mips' % (inf['width'], inf['height'], inf['format'],
                                     ' cube' if inf['cube'] else '', inf['mips'])) if inf else ''


def _norm(path):
    return os.path.normcase(os.path.abspath(bpy.path.abspath(path))) if path else ''


def image_at(path):
    """The loaded bpy image of a file, found by its path, or None. Not by name: a replacement named like the
    texture it replaces (McQueen_body_d.dds from another folder) is loaded as 'McQueen_body_d.dds.001', and
    a lookup by file name found the old image - the panel kept showing the old texture."""
    if not path:
        return None
    p = _norm(path)
    img = bpy.data.images.get(os.path.basename(path))
    if img is not None and img.filepath and _norm(img.filepath) == p:
        return img
    for img in bpy.data.images:
        if img.filepath and img.source == 'FILE' and _norm(img.filepath) == p:
            return img
    return None


def file_info(path):
    """'1024x1024 DXT1, 11 mips' for a texture file as the game will get it: a .dds as it is (export copies
    it verbatim), any other image as the uncompressed DDS export writes from it (RGB32, a full mip chain).
    '' when it can't be read."""
    p = bpy.path.abspath(path) if path else ''
    if not p or not os.path.exists(p):
        return ''
    if p.lower().endswith('.dds'):
        with open(p, 'rb') as f:
            return info_text(dds.info(f.read(128)))
    img = image_at(p)
    try:
        img = img or bpy.data.images.load(p, check_existing=True)
        w, h = img.size
    except RuntimeError:
        return ''
    # dds._mips halves down to 1x1: one level per bit of the longer side
    return '%dx%d RGB32, %d mips' % (w, h, max(w, h).bit_length()) if w and h else ''


def refresh_info(t):
    """Put the size / format line of a C2Texture's file (its replacement when it has one) in t.info, marked
    'new' (added in Blender) or 'replaced'."""
    s = file_info(t.replace or t.file)
    if not s:
        return
    if t.replace:
        s += ', new' if _norm(t.replace) == _norm(t.file) else ', replaced'
    t.info = s


def fill_material_props(mat, m, index):
    """Copy a MaterialPool entry `m` into mat.c2 (effect, parameters, texture slots).
    index = its MaterialPool index (-1 for a material not from this world).
    """
    c = mat.c2
    c.index = index
    c.effect = m.get('Effect', '')
    c.render_caps = m.get('RenderCaps', 0)
    c.props.clear()
    for pe in m.get('PropertyEntries', {}).values():
        p = c.props.add()
        p.name = pe.get('Name', '')
        p.ptype = pe.get('Type', 'Float')
        p.datatype = pe.get('DataType', 0)
        v = pe.get('Value')
        if isinstance(v, list):
            p.size = len(v)
            p.fvalue = (list(v) + [0.0] * 4)[:4]
        elif isinstance(v, float):
            p.size = 1
            p.fvalue = (v, 0, 0, 0)
        else:
            p.size = 1
            p.ivalue = int(v or 0)
    c.textures.clear()
    for te in m.get('TextureEntries', {}).values():
        t = c.textures.add()
        t.usage = te.get('Usage', '')
        t.texture = te.get('TextureReference', 0)


def write_props(m, c, n_textures):
    """Copy parameters and texture slots from mat.c2 into a MaterialPool entry.
    Only entries the material lists are changed, plus effect parameters added
    in Blender that differ from the effect's default.
    """
    pes = m.get('PropertyEntries')
    if pes is not None:
        byname = {p.name: p for p in c.props}
        listed = {pe.get('Name') for pe in pes.values()}
        for p in c.props:
            # parameters of a switched-to effect: written once they differ from its default
            if p.extra and p.name not in listed:
                vals = list(p.fvalue[:p.size]) if p.ptype == 'Float' else [float(p.ivalue)]
                if any(abs(a - b) > 1e-7 for a, b in zip(vals, p.fdefault)):
                    if p.ptype == 'Float':
                        v = float(vals[0]) if p.size == 1 else FloatList(float(x) for x in vals)
                    else:
                        v = int(p.ivalue)
                    pes['PropertyEntry#%d' % len(pes)] = {'Name': p.name, 'Type': p.ptype,
                                                           'DataType': p.datatype, 'Value': v}
        for pe in pes.values():
            p = byname.get(pe.get('Name'))
            if p is None:
                continue
            v = pe.get('Value')
            if isinstance(v, list):
                pe['Value'] = FloatList(float(x) for x in p.fvalue[:len(v)])
            elif isinstance(v, float):
                pe['Value'] = float(p.fvalue[0])
            elif isinstance(v, int):
                pe['Value'] = int(p.ivalue)
    # slots / parameters a shader switch dropped (fxnodes.switch_effect_entries):
    # kept on the Blender material, left out of the game's entry
    mat = c.id_data
    if mat.get('c2_dropped'):
        from . import fxnodes
        gone = fxnodes.dropped_cache(mat)
        have_t = {t.usage for t in c.textures}
        have_p = {p.name for p in c.props}
        for key, field, pool, have in (('TextureEntries', 'Usage', 'textures', have_t),
                                       ('PropertyEntries', 'Name', 'params', have_p)):
            ents = m.get(key)
            if ents is None:
                continue
            kept = [e for e in ents.values() if not (e.get(field) in gone[pool] and e.get(field) not in have)]
            if len(kept) != len(ents):
                prefix = key[:-3] + 'y#'      # TextureEntry#k / PropertyEntry#k
                m[key] = {'%s%d' % (prefix, i): e for i, e in enumerate(kept)}
    tes = m.get('TextureEntries')
    if tes is not None:
        byusage = {t.usage: t for t in c.textures}
        for te in tes.values():
            t = byusage.get(te.get('Usage'))
            if t is not None and 0 <= t.texture < n_textures:
                te['TextureReference'] = t.texture
        listed = {te.get('Usage') for te in tes.values()}
        for t in c.textures:
            if t.extra and t.usage not in listed and 0 <= t.texture < n_textures:
                tes['TextureEntry#%d' % len(tes)] = {'TextureReference': t.texture, 'Usage': t.usage}


def image_to_dds(img_path_or_image):
    """Image file path or bpy image -> DDS bytes. A .dds file is used as is;
    anything else is written from its pixels as uncompressed DDS.
    """
    if isinstance(img_path_or_image, str):
        p = bpy.path.abspath(img_path_or_image)
        if p.lower().endswith('.dds'):
            with open(p, 'rb') as f:
                return f.read()
        img = bpy.data.images.load(p, check_existing=True)
    else:
        img = img_path_or_image
        if img.filepath and img.filepath.lower().endswith('.dds') and os.path.exists(bpy.path.abspath(img.filepath)):
            with open(bpy.path.abspath(img.filepath), 'rb') as f:
                return f.read()
    w, h = img.size
    px = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(px)
    px = px.reshape(h, w, 4)[::-1]  # Blender rows are bottom-up
    return dds.write_2d(px)


def image_to_dds_white():
    """A 4x4 white DDS (fallback when an image can't be converted)."""
    return dds.write_2d(np.ones((4, 4, 4), np.float32))


def add_new_textures(texs, table, report, crc=None):
    """Textures added in Blender (Material panel > Load Image) take the next
    TexturePool indices, in index order: appended to `texs` (the pool's
    entries as a list). table: the C2Texture list. crc: DataCRC of new data
    (None: 0, as the Scene Editor writes worlds)."""
    for t in sorted(table, key=lambda t: t.index):
        if t.index < len(texs):
            continue
        if t.index != len(texs):
            report.warn('texture %s: index %d out of order, skipped' % (t.name, t.index))
            continue
        try:
            data = image_to_dds(t.replace or t.file)
        except Exception as e:
            report.warn('new texture %s failed: %s - using white' % (t.name, e))
            data = image_to_dds_white()
        data = dds.with_srgb(data, t.srgb)
        texs.append({'Name': t.name, 'DataCRC': crc(data) if crc else 0, 'SourceFilePath': t.source_path or
                     'materiallibrary\\custom\\%s.png' % t.name, 'Type': t.ttype or '2d', 'Data': data})
        report.info('new texture %d "%s"' % (t.index, t.name))


def apply_replacements(texs, table, orig_count, report, crc=None):
    """Put replaced files and changed sRGB flags of the original textures
    (index < orig_count) into their TexturePool entries."""
    for t in table:
        if not 0 <= t.index < orig_count:
            continue
        # files imported before the flag existed keep each texture's own
        srgb = t.srgb if t.is_property_set('srgb') else dds.is_srgb(texs[t.index].get('Data', b''))
        changed = False
        if t.replace:
            try:
                texs[t.index]['Data'] = image_to_dds(t.replace)
                changed = True
                report.info('texture %d "%s" replaced from %s' % (t.index, t.name, t.replace))
            except Exception as e:
                report.warn('texture %s replacement failed: %s' % (t.name, e))
        data = texs[t.index].get('Data', b'')
        if dds.is_srgb(data) != srgb:
            texs[t.index]['Data'] = dds.with_srgb(data, srgb)
            changed = True
        if changed and crc and 'DataCRC' in texs[t.index]:
            texs[t.index]['DataCRC'] = crc(texs[t.index]['Data'])
