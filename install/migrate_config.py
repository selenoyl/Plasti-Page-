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

# Old default page texts: (code, field, text). Version 0.1.0 / 0.2.0 wording.
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
]


def main(installed, example):
    with open(installed, encoding="utf-8") as f:
        cfg = json.load(f, object_pairs_hook=OrderedDict)
    with open(example, encoding="utf-8") as f:
        new = json.load(f, object_pairs_hook=OrderedDict)
    old = {(c, k): t for c, k, t in OLD_DEFAULTS}
    changes = []

    for code, ncode in new.get("codes", {}).items():
        icode = cfg.setdefault("codes", OrderedDict()).setdefault(code, OrderedDict(ncode))
        for field, value in ncode.items():
            if field not in icode:
                icode[field] = value
                changes.append("code %s: added %s" % (code, field))
            elif icode[field] != value and old.get((code, field)) == icode[field]:
                icode[field] = value
                changes.append("code %s %s: now \"%s\"" % (code, field, value))

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
