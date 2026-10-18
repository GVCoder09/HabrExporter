# Security policy

## Supported versions

Security fixes are released for the latest version only.

## Reporting a vulnerability

Please do not open a public issue. Report it privately through
[GitHub security advisories](https://github.com/GVCoder09/HabrExporter/security/advisories/new).

Include a description, steps to reproduce (for example, the article or API response that triggers
the problem) and the version you use. You will get a reply within a week. Once a fix is released,
the advisory is published with credit to you, unless you prefer otherwise.

## Scope

Habr Exporter downloads public content from habr.com and writes it to local files. Relevant problems
include script execution or loading of remote resources in saved HTML/EPUB files caused by article
or comment markup, writing files outside the chosen folder, and unsafe handling of downloaded images.
