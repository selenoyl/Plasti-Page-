#!/usr/bin/env python3
# P833 Call Receiver - bring an installed config.json up to date.
# By Ethan Brady & Ryan Raikes
#
# Run by install.sh:  migrate_config.py <installed config.json> <new example config>
# Only replaces text that is still an OLD DEFAULT, so wording you edited
# yourself is never touched. Adds settings that are new in this version.
# The installer has already backed up the old config before this runs.

import json
import sys
from collections import OrderedDict

# Old default page texts: (code, field, text). Versions 0.1.0 to 0.2.0
# ("needed at press") and 0.2.1 to 0.4.1 ("requested to press").
OLD_DEFAULTS = [
    ("2", "en", "Maintenance needed at press {press}."),
    ("2", "es", "Se necesita mantenimiento en la prensa {press}."),
    ("2", "en_no_press", "Maintenance needed. Press number unknown."),
    ("2", "es_no_press", "Se necesita mantenimiento. Prensa desconocida."),
    ("3", "en", "Technician needed at press {press}."),
    ("3", "es", "Se necesita un técnico en la prensa {press}."),
    ("3", "en_no_press", "Technician needed. Press number unknown."),
    ("3", "es_no_press", "Se necesita un técnico. Prensa desconocida."),
    ("4", "en", "Supervisor needed at press {press}."),
    ("4", "es", "Se necesita un supervisor en la prensa {press}."),
    ("4", "en_no_press", "Supervisor needed. Press number unknown."),
    ("4", "es_no_press", "Se necesita un supervisor. Prensa desconocida."),
    ("5", "en", "Quality needed at press {press}."),
    ("5", "es", "Se necesita calidad en la prensa {press}."),
    ("5", "en_no_press", "Quality needed. Press number unknown."),
    ("5", "es_no_press", "Se necesita calidad. Prensa desconocida."),
    ("6", "en", "Packaging needed at press {press}."),
    ("6", "es", "Se necesita empaque en la prensa {press}."),
    ("6", "en_no_press", "Packaging needed. Press number unknown."),
    ("6", "es_no_press", "Se necesita empaque. Prensa desconocida."),
    ("7", "en", "Supervisor required at press {press} for audit check-in."),
    ("7", "es", "Se requiere un supervisor en la prensa {press} para la revisión de auditoría."),
    ("7", "en_no_press", "Supervisor required for audit check-in. Press number unknown."),
    ("7", "es_no_press", "Se requiere un supervisor para la revisión de auditoría. Prensa desconocida."),
    ("8", "en", "Maintenance required at press {press} for camera fault."),
    ("8", "es", "Se requiere mantenimiento en la prensa {press} por falla de cámara."),
    ("8", "en_no_press", "Maintenance required for camera fault. Press number unknown."),
    ("8", "es_no_press", "Se requiere mantenimiento por falla de cámara. Prensa desconocida."),
    ("9", "en", "Maintenance required at press {press} for Red Rabbit failure."),
    ("9", "es", "Se requiere mantenimiento en la prensa {press} por falla de Red Rabbit."),
    ("9", "en_no_press", "Maintenance required for Red Rabbit failure. Press number unknown."),
    ("9", "es_no_press", "Se requiere mantenimiento por falla de Red Rabbit. Prensa desconocida."),
    ("2", "en", "Maintenance requested to press {press}."),
    ("2", "es", "Se solicita mantenimiento en la prensa {press}."),
    ("3", "en", "Technician requested to press {press}."),
    ("3", "es", "Se solicita un técnico en la prensa {press}."),
    ("4", "en", "Supervisor requested to press {press}."),
    ("4", "es", "Se solicita un supervisor en la prensa {press}."),
    ("5", "en", "Quality requested to press {press}."),
    ("5", "es", "Se solicita calidad en la prensa {press}."),
    ("6", "en", "Packaging requested to press {press}."),
    ("6", "es", "Se solicita empaque en la prensa {press}."),
    ("7", "en", "Supervisor requested to press {press} for audit check-in."),
    ("7", "es", "Se solicita un supervisor en la prensa {press} para la revisión de auditoría."),
    ("8", "en", "Maintenance requested to press {press} for camera fault."),
    ("8", "es", "Se solicita mantenimiento en la prensa {press} por falla de cámara."),
    ("9", "en", "Maintenance requested to press {press} for Red Rabbit failure."),
    ("9", "es", "Se solicita mantenimiento en la prensa {press} por falla de Red Rabbit."),
    # 0.4.2 to 0.6.0 ("press number" / "prensa número")
    ("2", "en", "Maintenance requested to press number {press}."),
    ("2", "es", "Se solicita mantenimiento en la prensa número {press}."),
    ("3", "en", "Technician requested to press number {press}."),
    ("3", "es", "Se solicita un técnico en la prensa número {press}."),
    ("4", "en", "Supervisor requested to press number {press}."),
    ("4", "es", "Se solicita un supervisor en la prensa número {press}."),
    ("5", "en", "Quality requested to press number {press}."),
    ("5", "es", "Se solicita calidad en la prensa número {press}."),
    ("6", "en", "Packaging requested to press number {press}."),
    ("6", "es", "Se solicita empaque en la prensa número {press}."),
    ("7", "en", "Supervisor requested to press number {press} for audit check-in."),
    ("7", "es", "Se solicita un supervisor en la prensa número {press} para la revisión de auditoría."),
    ("8", "en", "Maintenance requested to press number {press} for camera fault."),
    ("8", "es", "Se solicita mantenimiento en la prensa número {press} por falla de cámara."),
    ("9", "en", "Maintenance requested to press number {press} for Red Rabbit failure."),
    ("9", "es", "Se solicita mantenimiento en la prensa número {press} por falla de Red Rabbit."),
    # Department tones up to 0.12.0: Supervisor now has the bell, Maintenance the doorbell.
    ("2", "tone", "1_maintenance_bell"),
    ("8", "tone", "1_maintenance_bell"),
    ("9", "tone", "1_maintenance_bell"),
    ("4", "tone", "3_supervisor_glide"),
    ("7", "tone", "3_supervisor_glide"),
    # 0.12.1: Supervisor had the bell and Quality the flute; now swapped.
    ("4", "tone", "1_maintenance_bell"),
    ("7", "tone", "1_maintenance_bell"),
    ("5", "tone", "4_quality_flute"),
]


# Nested settings whose default changed: (section, key, old default).
OLD_NESTED_DEFAULTS = [
    ("audio", "keep_dry_run_pages", 20),   # 0.3.0 to 0.4.2 saved dry-run audio; now off
    ("audio", "gain_db", 6),               # 0.5.3 default; +8 was found clearer on the floor
    ("audio", "lead_silence_ms", 2500),    # 0.4.1 default; the feedback eliminator needs no lead-in
    ("phone", "wait_after_dial_ms", 2000),  # 1000 worked on the floor
]


def main(installed, example):
    with open(installed, encoding="utf-8") as f:
        cfg = json.load(f, object_pairs_hook=OrderedDict)
    with open(example, encoding="utf-8") as f:
        new = json.load(f, object_pairs_hook=OrderedDict)
    old = set(OLD_DEFAULTS)
    changes = []

    for code, ncode in new.get("codes", {}).items():
        icode = cfg.setdefault("codes", OrderedDict()).setdefault(code, OrderedDict(ncode))
        for field, value in ncode.items():
            if field not in icode:
                icode[field] = value
                changes.append("code %s: added %s" % (code, field))
            elif icode[field] != value and (code, field, icode[field]) in old:
                icode[field] = value
                changes.append("code %s %s: now \"%s\"" % (code, field, value))

    # Nested settings whose default changed: replace only if still the old default.
    for section, key, old_value in OLD_NESTED_DEFAULTS:
        if section in cfg and section in new and cfg[section].get(key) == old_value \
                and new[section].get(key, old_value) != old_value:
            cfg[section][key] = new[section][key]
            changes.append("%s.%s: now %s" % (section, key, json.dumps(new[section][key])))
        for nkey, nval in new.get(section, {}).items():      # new nested settings
            if section in cfg and nkey not in cfg[section] and not nkey.startswith("_"):
                cfg[section][nkey] = nval
                changes.append("added setting %s.%s" % (section, nkey))

    for key, value in new.items():
        if key not in cfg:
            cfg[key] = value
            changes.append("added setting %s" % key)

    if not changes:
        print("Config: already up to date")
        return 0
    tmp = installed + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    import os
    os.replace(tmp, installed)
    print("Config updated (your own edits kept):")
    for c in changes:
        print("  " + c)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
