import subprocess
import re
import semver


# Example usage
latest_tag = get_latest_semver_tag()
if latest_tag:
    print(f"Latest SemVer Tag: {latest_tag}")
else:
    print("No valid SemVer tags found.")

