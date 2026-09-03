"""Single source of truth for the app version.

Bump VERSION before each release. The GitHub release tag must match
(e.g. VERSION "1.0.0"  ->  git tag "v1.0.0").
The auto-updater compares this against the latest GitHub release.
"""

VERSION = "1.0.0"

# GitHub repo the updater checks for new releases.
GITHUB_OWNER = "thor8126"
GITHUB_REPO = "Aisha-AI"
