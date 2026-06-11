Now that you have a git repository that will behave as a [suede dependency](https://github.com/pmalacho-mit/suede), please perform the following tasks to complete the setup:

# TODO

- [ ] Set Actions permissions (_⚙️ Settings > ▶️ Actions > General > Workflow permissions_)
  > <img width="572" height="263" alt="Screenshot 2025-10-16 at 8 33 32 PM" src="https://github.com/user-attachments/assets/48c29bd0-de77-4d8e-84a0-87e9209d35b1" />
  - [ ] Read and write permissions
  - [ ] Allow GitHub Actions to create and approve pull requests
- [ ] _OPTIONAL (RECOMMENDED):_ Configure pull request head branches to automatically delete (_⚙️ Settings > ⚙️ General > Pull Requests_)
  >  <img width="987" height="163" alt="Screenshot 2025-11-18 at 8 41 01 AM" src="https://github.com/user-attachments/assets/522b7e98-ba2c-41df-aae6-8b3a3b5abac1" />
  - This is helpful to keep the branch list clean, as [one of the included github actions](https://github.com/pmalacho-mit/suede-dependency-template/blob/release/.github/workflows/subrepo-pull-into-main.yml) automatically creates "chore" pull requests
- [ ] Dispatch initialization workflow (_▶️ Actions > Initialization procedure > Run Workflow_)
  > <img width="1496" height="611" alt="Screenshot 2025-11-04 at 11 38 51 PM" src="https://github.com/user-attachments/assets/a32bcbc7-4ec3-492e-bf7a-1cc86db79f36" />
  - The action will:
    1. On the `release` branch, clone the suede `core` dependency into `.suede/core` (from the `dependency/release/core` branch) and push
    2. On the `main` branch, clone the suede `core` dependency into `.suede/core` (from the `dependency/main/core` branch), install [devcontainers-suede](https://github.com/pmalacho-mit/devcontainers-suede) into `.suede/devcontainers-suede`, link `.devcontainer/devcontainer.json`, then commit and push
    3. Clone a subrepo of the `release` branch into the `./release` folder within the `main` branch (so that changes within the `./release` folder of the `main` branch can be automatically synced to the `release` branch via [.github/workflows/subrepo-push-release.yml](https://github.com/pmalacho-mit/suede-dependency-template/blob/main/.github/workflows/subrepo-push-release.yml))
    4. Replaces the content of this README with specifics around installing this repository as a subrepo dependency
    5. 💥<em>SELF-DESTRUCT</em>💥 (meaning it will delete [.github/workflows/initialize.yml](https://github.com/pmalacho-mit/suede-dependency-template/blob/main/.github/workflows/initialize.yml))
  > [!NOTE]
  > **Why does init clone `core`/`devcontainers` in instead of the template shipping them directly?** So that each dependency's `.gitrepo` file points at a real commit of its actual source repository — a baked-in copy would have no valid subrepo link to pull/push against. The GitHub Actions **workflow** files (under `.github/workflows`), by contrast, *are* baked into the template: GitHub refuses to let an action create or modify workflow files using the default `GITHUB_TOKEN` (it would require a higher-privileged PAT, which this setup intentionally avoids).
