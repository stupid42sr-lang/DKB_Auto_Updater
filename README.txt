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
Draft development releases may be selected because allow_prerelease is explicit in config.
For bundled installations, managed component changes use the same SHA256/rollback flow.
Skipped releases use matching intermediate deltas; no automatic Full download.
Packaged updater runs from temporary embedded Python so installed DLLs can be replaced.
