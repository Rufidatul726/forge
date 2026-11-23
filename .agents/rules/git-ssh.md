---
trigger: always_on
description: Git SSH configuration for repository pushes and remote operations
---

# Git SSH Configuration

When fetching, pulling, or pushing to GitHub (`git@github.com:Rufidatul726/forge.git`), use the personal SSH key:

```bash
git -c core.sshCommand="ssh -i ~/.ssh/id_ed25519_personal -o IdentitiesOnly=yes" <command>
```
