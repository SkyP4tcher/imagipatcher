#!/usr/bin/env python3
"""imagipatcher: adds to the recompiled Skylanders Imaginators Xbox 360 the characters and vehicles
found only in the Wii U version, converting them from the Wii U files.

Imaginators only. It contains no game files or keys and decrypts nothing: it works on the Wii U game
files already extracted by the user and on the user's Xbox 360 game. The languages are the ones on the
Xbox 360 game (the European Wii U release has all of them).

Usage:
  python3 imagipatcher.py --wiiu <Wii U files folder> --x360 <recompiled 360 game folder>
                          --output <folder> [--test-copy <new folder>] [--languages es,it]

--wiiu        extracted Wii U game folder (with content/archives) or directly its archives folder.
              Read only.
--x360        recompiled installation folder (with rom/archives) or directly the archives folder of
              the Xbox 360 game. Read only.
--output      receives archives/ with the converted paks and a summary.
--test-copy   creates a separate installation: links to the original files, converted paks in place,
              memory pools and portal option set (see --apply-config) and a .bat that uses separate
              save files.
--languages   forces the languages instead of reading them from the Xbox 360 game.
--apply-config <Core of Light folder>: only sets rom/MemoryPoolConfigs.igx, rom/VramBudget.igx and
              portal_menu_nintendo in an existing installation, keeping the originals as .orig.

Requires Python 3 with numpy and scipy, and ffmpeg (xma2 decoder) to verify the audio.
"""
import argparse, os, re, shutil, struct, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from concurrent.futures import ProcessPoolExecutor
import iga, igawrite, conv, fsb, xma, xma_ms, wfix_lib
from igz import fnv1a

ALL_LANGS = conv.ALL_LANGS

# ---------------------------------------------------------------- content
TOY_PAKS = ['Bowser', 'Bowser_abilityupgrades', 'DonkeyKong', 'DonkeyKong_abilityupgrades',
            'ClownPlane', 'ClownPlane_blueprints', 'DKCart', 'DKCart_blueprints']
VIDEOS = []                     # Imaginators has no Soul Gem videos for these toys
HERO_SLOT = 3 * 1024 * 1024
VEHICLE_SLOT = 1536 * 1024
# sound banks loaded together: (slot, main paks, prefix of the per-language voice paks)
# Imaginators splits the character sound slot (MemoryPoolConfigs.igx, Xbox 360): 2 MB for the main bank
# (ShapeshifterSoundBody) and 1 MB for the voices (ShapeshifterSoundLegs). SuperChargers has a single 3 MB slot.
SOUND_BODY_SLOT = 2 * 1024 * 1024
SOUND_LEGS_SLOT = 768 * 1024          # kept below the 1 MB pool, as the native voice banks are
AUDIO_GROUPS = {
    'Bowser': (SOUND_BODY_SLOT, ['CHAMP_L15_Bowser_bank'], None),
    'Bowser voices': (SOUND_LEGS_SLOT, ['CHAMP_L15_Bowser_bank_v'], 'CHAMP_L15_Bowser_bank_Lv'),
    'Bowser giant voices': (SOUND_LEGS_SLOT, ['CHAMP_L15_Bowser_Giant_bank_v'], None),
    'DonkeyKong': (SOUND_BODY_SLOT, ['CHAMP_L15_DonkeyKong_bank'], None),
    'DonkeyKong voices': (SOUND_LEGS_SLOT, ['CHAMP_L15_DonkeyKong_bank_v'], 'CHAMP_L15_DonkeyKong_bank_Lv'),
    'ClownPlane': (VEHICLE_SLOT, ['CHAMP_V15_BowserPlane_bank'], 'CHAMP_V15_BowserPlane_bank_Lv'),
    'DKCart': (VEHICLE_SLOT, ['CHAMP_V15_DKCart_bank'], 'CHAMP_V15_DKCart_bank_Lv'),
}
AUDIO_FRAC = 0.86                 # native Imaginators banks stay around 1.8-1.9 MB of the 2 MB slot
PERMANENT_PKG = 'packages/generated/packageXmls/permanent_pkg.igz'
SECTOR_AUDIO = 0x800

EXE_NAME = 'imaginatorsrecompiled.exe'


TOML_NAME = 'imaginatorsrecomp.toml'
VRAM_NAME = 'VramBudget.igx'
VRAM_XENON = {'_character': '14', '_vehicle': '14'}   # MB per character and vehicle (was 8 and 10)
# Imaginators reads the character and vehicle slot sizes from rom/MemoryPoolConfigs.igx (SuperChargers has
# them in the executable). The character data slot gets 1.5 MB more and the vehicle data slot 1 MB more,
# enough for the larger character and vehicle. All these pools sit in a fixed area: the space comes from
# the Story mode sound sample pool (3 MB less). Making the area itself bigger freezes the game at startup.
MEMPOOL_NAME = 'MemoryPoolConfigs.igx'
# (object, field, original value, new value); field None = the IG_CORE_PLATFORM_XENON entry
MEMPOOL_XENON = [('ShapeshifterDataLegsBudget_Story', None, 1587200, 3145728),
                 ('ShapeshifterDataLegsBudget_ActionPack', None, 1587200, 3145728),
                 ('VehicleDataBudget', None, 1843200, 2891776),
                 ('MemoryPool_Story_xenon_MP_SOUND_SAMPLE', '_poolSize', 18874368, 15728640)]


def patch_mempool(b):
    """Character slot and the pool that pays for it, Xbox 360 values. Returns (bytes or None, message)."""
    t = b.decode('utf-8')
    for ref, field, old, new in MEMPOOL_XENON:
        m = re.search(r'<object [^>]*refname="%s"[^>]*>.*?</object>' % ref, t, re.S)
        if not m:
            return None, 'memory pools NOT modified: %s not found' % ref
        blk = m.group(0)
        if field is None:
            v = re.search(r'<dictitem key="IG_CORE_PLATFORM_XENON" value="(\d+)"', blk)
        else:
            v = re.search(r'<var name="%s" value="(\d+)"' % field, blk)
        if not v or int(v.group(1)) not in (old, new):
            return None, 'memory pools NOT modified: unexpected value in %s' % ref
        blk = blk[:v.start(1)] + str(new) + blk[v.end(1):]
        t = t[:m.start()] + blk + t[m.end():]
    return t.encode('utf-8'), 'memory pools: character slot +1.5 MB, vehicle slot +1 MB, from the Story sound sample pool'


def patch_vram(b):
    """Xbox 360 video memory budget per character and vehicle: the added toys' textures are at full
    quality. Only the values in the XENON entry change."""
    t = b.decode('utf-8')
    m = re.search(r'(<object refname="VramBudget_IG_CORE_PLATFORM_XENON".*?</object>)', t, re.S)
    if not m:
        return b
    blk = m.group(1)
    for k, v in VRAM_XENON.items():
        blk = re.sub(r'(<var name="%s" value=")[^"]*(")' % k, r'\g<1>%s\g<2>' % v, blk)
    return (t[:m.start()] + blk + t[m.end():]).encode('utf-8')


def log(*a):
    print(*a, flush=True)


def lang_of(name):
    m = re.search(r'_([a-z]{2})$', name.rsplit('.', 1)[0])
    return m.group(1) if m and m.group(1) in ALL_LANGS else None


def wanted_lang(name, langs):
    lg = lang_of(name)
    return lg is None or lg in langs


# ---------------------------------------------------------------- Wii U files
def find_wiiu(path):
    for p in (os.path.join(path, 'content', 'archives'), os.path.join(path, 'archives'), path):
        if os.path.exists(os.path.join(p, 'Bowser.pak')):
            return p
    raise SystemExit('Bowser.pak not found in %s: extracted Imaginators Wii U is needed' % path)


# ---------------------------------------------------------------- Xbox 360 game
def find_archives(path):
    for p in (os.path.join(path, 'rom', 'archives'), os.path.join(path, 'archives'), path):
        if os.path.exists(os.path.join(p, 'permanent.pak')):
            return p
    raise SystemExit('permanent.pak not found in ' + path)


def langs_360(arch):
    names = [n for _, n in igawrite.iga_names(os.path.join(arch, 'permanent.pak'))]
    langs = sorted(set(m.group(1) for n in names for m in [re.fullmatch(r'LooseData/systemData_([a-z]{2})\.lng', n)] if m))
    if not langs:
        raise SystemExit('Xbox 360 game languages not recognized')
    return langs


# ---------------------------------------------------------------- IGZ and pak
def convert_entry(name, data, langs):
    if not wanted_lang(name, langs):
        return None
    if data[:4] == b'IGZ\x01':
        doc = conv.convert(data, name)
        if name.startswith('actors/Vehicles/'):
            wfix_lib.fix(doc)        # the 360 rewrites the first weight to 255: the largest weight goes there
        return doc.serialize()
    return data


def pak_params(path):
    b = open(path, 'rb').read(0x38)
    return struct.unpack_from('>I', b, 0x10)[0], struct.unpack_from('>I', b, 0x34)[0]


def write_pak(dst, files, sector, flags):
    igawrite.write_iga(dst + '.part', files, sector=sector, flags=flags)
    os.replace(dst + '.part', dst)


def convert_pak(src, dst, langs):
    sector, flags = pak_params(src)
    files = []
    for logical, bpath, data, stored in igawrite.entry_list(src):
        d = convert_entry(logical, data, langs) if '/cafe/' in bpath else data
        if d is None:
            continue
        files.append((logical, bpath.replace('/BuildServer/cafe/', '/BuildServer/xenon/'), d, 'stored' if stored else 'zlib'))
    write_pak(dst, files, sector, flags)
    return files


def build_permanent(arch, wiiu, dst, langs):
    src = os.path.join(arch, 'permanent.pak')
    sector, flags = pak_params(src)
    have = set(n.lower() for _, n in igawrite.iga_names(src))
    wl = igawrite.entry_list(os.path.join(wiiu, 'permanent.pak'))
    files = []; added = []
    for logical, bpath, data, stored in igawrite.entry_list(src):
        if logical == PERMANENT_PKG:
            data = conv.convert([e for e in wl if e[0] == PERMANENT_PKG][0][2], PERMANENT_PKG).serialize()
        files.append((logical, bpath, data, 'stored' if stored else 'zlib'))
    for logical, bpath, data, stored in wl:
        if logical.lower() in have or not wanted_lang(logical, langs):
            continue
        d = conv.convert(data, logical).serialize() if data[:4] == b'IGZ\x01' else data
        files.append((logical, bpath.replace('/BuildServer/cafe/', '/BuildServer/xenon/'), d, 'stored' if stored else 'zlib'))
        added.append(logical)
    write_pak(dst, files, sector, flags)
    return files, added


# ---------------------------------------------------------------- audio
def mp3_decode(data, channels, samples, work):
    import subprocess
    src = os.path.join(work, 'in.mp3'); raw = os.path.join(work, 'out.raw')
    open(src, 'wb').write(data)
    r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'mp3', '-i', src,
                        '-f', 's16le', '-acodec', 'pcm_s16le', raw], capture_output=True, text=True)
    if r.returncode or r.stderr.strip():
        raise SystemExit('ffmpeg mp3: ' + r.stderr.strip())
    y = np.fromfile(raw, '<i2').reshape(-1, channels)
    if len(y) >= samples:
        return y[:samples]
    return np.concatenate([y, np.zeros((samples - len(y), channels), np.int16)])


def source_pcm(f, work):
    if f['codec'] == fsb.CODEC_IMA:
        return fsb.ima_decode(f['data'], f['channels'], f['samples'])
    if f['codec'] == fsb.CODEC_PCM16:
        return np.frombuffer(f['data'][:f['samples'] * 2 * f['channels']], '<i2').reshape(-1, f['channels'])
    if f['codec'] == fsb.CODEC_MPEG:
        return mp3_decode(f['data'], f['channels'], f['samples'], work)
    raise SystemExit('audio codec %d not supported' % f['codec'])


def load_bank(path, work, dc=False):
    out = []
    for logical, bpath, data, stored in igawrite.entry_list(path):
        f = fsb.parse(data)
        loop = 'loop' in logical.lower()
        if dc:
            loop = loop or logical.split('/')[-1].startswith('INTER_') and not logical.lower().endswith('intro')
        out.append(dict(logical=logical, f=f, loop=loop, pcm=xma.prepare_pcm(source_pcm(f, work), f['rate'], loop)))
    return out


def _encode_stream(args):
    pcm, noise, loop = args
    return xma.encode_frames(pcm, xma.Params(noise=noise), loop)


def encode_fsb(e, noise, pool=None):
    """XMA FSB of a sound; above 2 channels one stream per channel pair (xma_ms)."""
    pcm = e['pcm']; nch = pcm.shape[1]
    if nch <= 2:
        data, c6, _ = xma.encode(pcm, xma.Params(noise=noise), e['loop'])
    else:
        lay = xma_ms.stream_layout(nch); parts = []; c = 0
        for n in lay:
            parts.append(np.ascontiguousarray(pcm[:, c:c + n])); c += n
        mp = pool.map if pool else map
        frames = list(mp(_encode_stream, [(p, noise, e['loop']) for p in parts]))
        data, counts = xma_ms.interleave(frames)
        c6 = xma_ms.seek_chunk(len(lay), counts)
    return xma.to_fsb(e['f'], data, c6, len(pcm), e['f']['channels'])


def fsb_size(d):
    return (len(d) + SECTOR_AUDIO - 1) // SECTOR_AUDIO * SECTOR_AUDIO


def _encode_refine(args):
    e, noise, refine = args
    d = encode_fsb(e, noise)
    if refine:                       # raise quality while it stays in the same sectors
        size = fsb_size(d); n = noise
        for _ in range(8):
            n *= 0.85
            d2 = encode_fsb(e, n)
            if fsb_size(d2) > size:
                break
            d = d2
    return d


def verify_fsb(d, pcm, work):
    """Identical re-read and rewrite of every stream, ffmpeg decode without errors and with the right
    length. Returns (ok, message, minimum SNR)."""
    g = fsb.parse(d)
    if g['codec'] != fsb.CODEC_XMA:
        return False, 'codec %d' % g['codec'], 0
    lay = xma_ms.stream_layout(g['channels'])
    if len(lay) == 1:
        ok, _ = xma.check_stream(g['data'], g['channels'])
        if not ok:
            return False, 'rewrite differs', 0
        streams = [g['data']]
    else:
        bl = xma_ms.split_streams(g['data'], len(lay))
        offs = xma.band_offsets(xma.RATE)
        fr = [xma_ms.stream_frames(bl[s], lay[s], xma.RATE) for s in range(len(lay))]
        fb = [[xma.write_frame(f, lay[s], offs) for b in fr[s] for f in b] for s in range(len(lay))]
        data, counts = xma_ms.interleave(fb)
        if data != g['data'] or xma_ms.seek_chunk(len(lay), counts) != dict(g['chunks'])[6]:
            return False, 'multistream rewrite differs', 0
        streams = []
        for blocks in bl:
            s = bytearray(b''.join(blocks))
            for k in range(0, len(s), xma.PKT):
                s[k + 3] = 0
            streams.append(bytes(s))
    snrs = []; c = 0
    for n, sd in zip(lay, streams):
        y, err = xma.ffmpeg_decode(sd, n, g['samples'], work)
        if err:
            return False, 'ffmpeg: ' + err[:200], 0
        if len(y) != g['samples']:
            return False, 'length %d instead of %d' % (len(y), g['samples']), 0
        snrs.append(xma.snr_db(pcm[:, c:c + n], y)); c += n
    return True, '', min(snrs)


def write_bank(src, dst, entries, encoded, work, min_snr):
    """Audio pak on the structure of the Wii U pak (hashes, names and chain kept), after verification."""
    new = {}; snrs = []
    for e, d in zip(entries, encoded):
        ok, msg, snr = verify_fsb(d, e['pcm'], work)
        silent = float(np.max(np.abs(e['pcm']))) < 1.0
        if not ok or (snr < min_snr and not silent):
            raise SystemExit('%s %s: audio check failed (%s, SNR %.1f)' % (os.path.basename(src), e['logical'], msg, snr))
        new[e['logical']] = d
        if not silent:
            snrs.append(snr)
    igawrite.replace_iga(src, dst + '.part', new)
    os.replace(dst + '.part', dst)
    return (min(snrs), float(np.mean(snrs))) if snrs else (0, 0)


def pak_bytes(encoded, src):
    """Size of the pak written by replace_iga: header and names of the source, data in sectors."""
    b = open(src, 'rb').read(0x38)
    n = struct.unpack_from('>I', b, 0xC)[0]
    head = 0x38 + n * 20; head += (-head) % SECTOR_AUDIO
    return head + sum(fsb_size(d) for d in encoded) + struct.unpack_from('>I', b, 0x30)[0]


def build_toy_audio(wiiu, out, langs, pool, work):
    report = []
    for g, (slot, paks, lv) in AUDIO_GROUPS.items():
        lvs = ['%s_%s' % (lv, l) for l in langs] if lv else []
        names = paks + lvs
        src = {p: load_bank(os.path.join(wiiu, p + '.pak'), work) for p in names}
        def group_size(enc):
            size = lambda p: pak_bytes(enc[p], os.path.join(wiiu, p + '.pak'))
            return sum(size(p) for p in paks) + max([size(p) for p in lvs] or [0])
        def encode_all(noise, refine=False):
            return {p: list(pool.map(_encode_refine, [(e, noise, refine) for e in src[p]], chunksize=1)) for p in names}
        lo, hi = 0.1, 4.0            # common noise for the group: the lowest that fits in the slot
        for _ in range(8):
            mid = (lo * hi) ** 0.5
            if group_size(encode_all(mid)) > AUDIO_FRAC * slot:
                lo = mid
            else:
                hi = mid
        enc = encode_all(hi, refine=True)
        total = group_size(enc)
        if total > slot:
            raise SystemExit('%s: audio too large for the slot (%d > %d)' % (g, total, slot))
        for p in names:
            mn, avg = write_bank(os.path.join(wiiu, p + '.pak'), os.path.join(out, p + '.pak'), src[p], enc[p], work, 3)
            report.append('%-36s %3d sounds  SNR min %.1f mean %.1f dB' % (p, len(src[p]), mn, avg))
        report.append('== %s: %d bytes, %.1f%% of the slot' % (g, total, 100.0 * total / slot))
        log(report[-1])
    return report


# ---------------------------------------------------------------- checks
def check_pak(dst, files, langs, base=None):
    """Re-read: same data, sorted FNV hashes, data chain, 360 IGZ, no Wii U paths or languages.
    base: starting 360 pak, whose entries stay even in languages other than the chosen ones."""
    keep = set(n for _, n in igawrite.iga_names(base)) if base else set()
    f2, d2 = iga.read_iga(dst)
    got = {x[0]: d2(x) for x in f2}
    if got != {f[0]: f[2] for f in files}:
        raise SystemExit('%s: re-read data differs' % dst)
    hs = igawrite.iga_hashes(dst); names = igawrite.iga_names(dst)
    if hs != sorted(hs) or not all(h == fnv1a(n[1].lower()) for h, n in zip(hs, names)):
        raise SystemExit('%s: hash' % dst)
    if not igawrite.check_chain(dst):
        raise SystemExit('%s: entry chain' % dst)
    for n, d in got.items():
        if d[:4] == b'IGZ\x01' and struct.unpack_from('>I', d, 0xC)[0] != 5:
            raise SystemExit('%s: %s is not for 360' % (dst, n))
        if not wanted_lang(n, langs) and n not in keep:
            raise SystemExit('%s: language not present on the 360 game (%s)' % (dst, n))
    if any('/cafe/' in n[0] for n in names):
        raise SystemExit('%s: Wii U paths left' % dst)
    return len(got)


def check_audio_pak(dst, src):
    if igawrite.iga_hashes(dst) != igawrite.iga_hashes(src) or igawrite.iga_names(dst) != igawrite.iga_names(src):
        raise SystemExit('%s: hashes or names differ from the Wii U pak' % dst)
    if not igawrite.check_chain(dst):
        raise SystemExit('%s: entry chain' % dst)
    f2, d2 = iga.read_iga(dst)
    if any(fsb.parse(d2(x))['codec'] != fsb.CODEC_XMA for x in f2):
        raise SystemExit('%s: audio is not XMA' % dst)


# ---------------------------------------------------------------- test copy
def link_or_copy(src, dst):
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def make_test_copy(root, out_arch, dest):
    if not os.path.exists(os.path.join(root, EXE_NAME)):
        raise SystemExit('--test-copy requires the recompiled installation folder in --x360')
    if os.path.exists(dest):
        raise SystemExit('%s already exists: give a new folder' % dest)
    for d, _, fs in os.walk(root):
        rel = os.path.relpath(d, root)
        os.makedirs(os.path.join(dest, rel), exist_ok=True)
        for f in fs:
            link_or_copy(os.path.join(d, f), os.path.join(dest, rel, f))
    arch = os.path.join(dest, 'rom', 'archives')
    for f in os.listdir(out_arch):
        t = os.path.join(arch, f)
        if os.path.exists(t):
            os.remove(t)                 # drop the link: the original does not change
        shutil.copyfile(os.path.join(out_arch, f), t)
    msg = apply_config(dest, backup=False)
    open(os.path.join(dest, 'Launch Imaginators imagipatcher.bat'), 'w', newline='\r\n').write(
        '@echo off\ncd /d "%~dp0"\nstart "" ' + EXE_NAME +
        ' --user_data_root="%USERPROFILE%\\Documents\\Skylanders\\Imaginators imagipatcher"\n')
    return msg


def rewrite(path, new, backup):
    """Replaces a file (or the link to it) with new content, keeping the first original as .orig."""
    if backup and not os.path.exists(path + '.orig'):
        shutil.copyfile(path, path + '.orig')
    os.remove(path)
    open(path, 'wb').write(new)


def toml_portal(t):
    if re.search(r'^\s*portal_menu_nintendo\s*=', t, re.M):
        return re.sub(r'^(\s*portal_menu_nintendo\s*=\s*)\w+', r'\1true', t, flags=re.M)
    lines = t.split('\n')
    sec = [k for k, l in enumerate(lines) if re.fullmatch(r'\s*\[\s*"?Portal"?\s*\]\s*', l)]
    if sec:
        lines.insert(sec[0] + 1, 'portal_menu_nintendo = true')
    else:
        lines += ['', '["Portal"]', 'portal_menu_nintendo = true']
    return '\n'.join(lines)


def apply_config(root, backup=True):
    """Memory pools, video memory budget and portal menu option of a Core of Light folder."""
    msgs = []
    mp = os.path.join(root, 'rom', MEMPOOL_NAME)
    if not os.path.exists(mp):
        raise SystemExit('%s not found in %s' % (MEMPOOL_NAME, os.path.join(root, 'rom')))
    t = open(mp, 'rb').read()
    patched, m = patch_mempool(t)
    if patched and patched != t:
        rewrite(mp, patched, backup)
    msgs.append(m)
    vb = os.path.join(root, 'rom', VRAM_NAME)
    if os.path.exists(vb):
        t = open(vb, 'rb').read()
        new = patch_vram(t)
        if new != t:
            rewrite(vb, new, backup)
        msgs.append('video memory budget: characters and vehicles 14 MB')
    toml = os.path.join(root, TOML_NAME)
    if os.path.exists(toml):
        t = open(toml, encoding='utf-8').read()
        new = toml_portal(t)
        if new != t:
            rewrite(toml, new.encode('utf-8'), backup)
        msgs.append('%s: portal_menu_nintendo = true' % TOML_NAME)
    return '\n'.join(msgs)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description='Wii U characters and vehicles in the recompiled Imaginators 360')
    ap.add_argument('--wiiu', help='extracted Imaginators Wii U folder')
    ap.add_argument('--x360', help='Core of Light folder with Imaginators')
    ap.add_argument('--output', help='folder for the modified files')
    ap.add_argument('--test-copy', help='new folder for a separate installation')
    ap.add_argument('--languages', help='e.g. es,it (default: from the Xbox 360 game)')
    ap.add_argument('--jobs', type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument('--apply-config', metavar='FOLDER',
                    help='only set the memory pools, video memory budget and portal option in a Core of '
                         'Light folder, for example after an update (the originals are kept as .orig)')
    a = ap.parse_args()
    if a.apply_config:
        print(apply_config(a.apply_config))
        return
    if not (a.wiiu and a.x360 and a.output):
        ap.error('--wiiu, --x360 and --output are required')
    t0 = time.time()
    import subprocess
    try:
        dec = subprocess.run(['ffmpeg', '-hide_banner', '-decoders'], capture_output=True, text=True).stdout
    except OSError:
        dec = ''
    if ' xma2 ' not in dec:
        raise SystemExit('ffmpeg with the xma2 decoder is required (to verify the converted audio)')

    arch = find_archives(a.x360)
    langs = a.languages.split(',') if a.languages else langs_360(arch)
    bad = [l for l in langs if l not in ALL_LANGS]
    if bad:
        raise SystemExit('unknown languages: %s' % bad)
    conv.LANGS_360 = tuple(langs)
    log('Xbox 360 game: %s, languages %s' % (arch, ','.join(langs)))

    wiiu = find_wiiu(a.wiiu)
    out = os.path.join(a.output, 'archives')
    need = [p + '.pak' for p in TOY_PAKS + ['permanent']]
    for g, (_, paks, lv) in AUDIO_GROUPS.items():
        need += [p + '.pak' for p in paks] + (['%s_%s.pak' % (lv, l) for l in langs] if lv else [])
    need += VIDEOS
    missing = [n for n in need if not os.path.exists(os.path.join(wiiu, n))]
    if missing:
        raise SystemExit('Wii U files missing in %s: %s' % (wiiu, ', '.join(missing)))
    log('Wii U files: %s, all %d required files present' % (wiiu, len(need)))
    os.makedirs(out, exist_ok=True)
    work = tempfile.mkdtemp(prefix='imagipatcher_')

    report = ['languages: ' + ','.join(langs)]
    # 2. toys and vehicles
    for p in TOY_PAKS:
        files = convert_pak(os.path.join(wiiu, p + '.pak'), os.path.join(out, p + '.pak'), langs)
        report.append('%-36s %4d entries' % (p, check_pak(os.path.join(out, p + '.pak'), files, langs)))
        log(report[-1])
    for v in VIDEOS:
        shutil.copyfile(os.path.join(wiiu, v), os.path.join(out, v))
    # 3. figure registry
    files, added = build_permanent(arch, wiiu, os.path.join(out, 'permanent.pak'), langs)
    check_pak(os.path.join(out, 'permanent.pak'), files, langs, os.path.join(arch, 'permanent.pak'))
    report.append('permanent.pak: %d entries added, permanent_pkg from Wii U' % len(added))
    log(report[-1])
    # 4. audio to XMA
    audio_work = os.path.join(work, 'audio')
    os.makedirs(audio_work, exist_ok=True)
    with ProcessPoolExecutor(a.jobs) as pool:
        report += build_toy_audio(wiiu, out, langs, pool, audio_work)
    for f in os.listdir(out):
        if f.endswith('.pak') and f.startswith('CHAMP_'):
            check_audio_pak(os.path.join(out, f), os.path.join(wiiu, f))
    first = len(report)
    report.append('checks passed on %d files' % len(os.listdir(out)))
    if a.test_copy:
        report.append(make_test_copy(os.path.abspath(a.x360), out, a.test_copy))
        report.append('test copy: ' + a.test_copy)
    report.append('time: %.0f s' % (time.time() - t0))
    open(os.path.join(a.output, 'summary.txt'), 'w', encoding='utf-8').write('\n'.join(report) + '\n')
    log('\n'.join(report[first:]))
    shutil.rmtree(work, ignore_errors=True)   # temporary folder created here


if __name__ == '__main__':
    main()
