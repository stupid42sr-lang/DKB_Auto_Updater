Public distribution repository for DKB only.
Project sources live in the Private DKB_Auto repository.
Release assets contain only project payloads, manifests and SHA256SUMS.txt.
Shared Base/Runtime/Python/DLL/models and user credentials/settings are not bundled.

update.cmd --install C:\Vampire\DKB_Auto --github --check
update.cmd --install C:\Vampire\DKB_Auto --github
update.cmd --install C:\Vampire\DKB_Auto --zip <delta.zip> --sha256 <hash>
update.cmd --install C:\Vampire\DKB_Auto --recover
update.cmd --install C:\Vampire\DKB_Auto --finish

Keep the app closed while updating. The existing project mutex is checked.
Only matching-baseline deltas apply. Unknown/local edits block updates.
User license/settings/logs remain untouched. Rollback backup is retained until --finish.
Draft development releases may be selected because allow_prerelease is explicit in config.
The current Runtime stays pinned; a Runtime change is a separate install/release.
