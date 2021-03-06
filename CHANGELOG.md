# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For each Pull Request, the affected code parts should be briefly described and added here in the "Upcoming" section.
Once a release is done, the "Upcoming" section becomes the release changelog, and a new empty "Upcoming" should be
created.
 
## Upcoming

### Added

### Changed
- The arguments of the `score.py` script changed: `data_root` -> `data_folder`, it no longer assumes a fixed
`data` subfolder. `project_root` -> `model_root`, `test_image_channels` -> `image_files`.
- By default, the visualization of patch sampling for segmentation models will run on only 1 image (down from 5).
This is because patch sampling is expensive to compute, taking 1min per large CT scan.

### Fixed
- When registering a model, it now has a consistent folder structured, described [here](docs/deploy_on_aml.md). This
folder structure is present irrespective of using InnerEye as a submodule or not. In particular, exactly 1 Conda
environment will be contained in the model.

### Removed
- Removed blobxfer completely. AzureML Data-stores for reading datasets make the following configs obsolete: 'datasets_storage_account' and 'datasets_storage_account_key' and they are not longer supported. 

### Deprecated



## 0.1 (2020-11-13)
- This is the baseline release.
