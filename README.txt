Public distribution repository for DKB only.
Project sources live in the Private DKB_Auto repository.
Initial Full contains versioned sibling project/Base/Runtime/engine4/updater folders.
Subsequent releases contain changed-file deltas, manifests and SHA256SUMS.txt.
Real user credentials/settings are never bundled; Full provides an empty license.dat.

update.cmd --install C:\Vampire\DKB_Auto --github --check
update.cmd --install C:\Vampire\DKB_Auto --github
update.cmd --install C:\Vampire\DKB_Auto --zip <delta.zip> --sha256 <hash>
update.cmd --install C:\Vampire\DKB_Auto --recover
update.cmd --install C:\Vampire\DKB_Auto --finish

Keep the app closed while updating. The existing project mutex is checked.
Only matching-baseline deltas apply. Unknown/local edits block updates.
User license/settings/logs remain untouched. Rollback backup is retained until --finish.
New STABLE clients ignore prereleases, even if an old config still says allow_prerelease=true.
Both channels use stupid42sr-lang/DKB_Auto_Updater. DEV creates no Release/Pre-release.
DEV reads DEV_index.json directly on the existing STABLE tag configured by dev_anchor_tag.
Only DEV_ assets are added. Existing STABLE assets/names/hashes are never changed or deleted.
Legacy clients still see the same Release tags and fixed STABLE asset names; they ignore DEV_ assets.
DEV requires an installation-local settings/update-channel.json with contract=dkb-update-channel-v1
and channel=DEV. Missing/invalid settings default to STABLE. No device ID/allowlist is needed.
The legacy dev-devices.json is ignored. License/Manager device identity is unchanged.
For bundled installations, managed component changes use the same SHA256/rollback flow.
Skipped releases use matching intermediate deltas; no automatic Full download.
Packaged updater runs from temporary embedded Python so installed DLLs can be replaced.
