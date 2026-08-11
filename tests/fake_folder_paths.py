"""
A minimal stand-in for ComfyUI's folder_paths module — just enough surface
area for core/*.py to import and run against in isolation, with no real
ComfyUI install needed. Each test file installs one of these into
sys.modules['folder_paths'] *before* importing anything under core/.
"""

import os
import types


def make_fake_folder_paths(base_path: str):
    fp = types.ModuleType("folder_paths")
    fp.base_path = base_path
    fp.models_dir = os.path.join(base_path, "models")
    fp.folder_names_and_paths = {}

    def add_model_folder_path(folder_name, full_folder_path, is_default=False):
        if folder_name in fp.folder_names_and_paths:
            paths, exts = fp.folder_names_and_paths[folder_name]
            if full_folder_path in paths:
                if is_default and paths[0] != full_folder_path:
                    paths.remove(full_folder_path)
                    paths.insert(0, full_folder_path)
            elif is_default:
                paths.insert(0, full_folder_path)
            else:
                paths.append(full_folder_path)
        else:
            fp.folder_names_and_paths[folder_name] = ([full_folder_path], set())

    def get_folder_paths(folder_name):
        return fp.folder_names_and_paths[folder_name][0][:]

    def recursive_search(directory, excluded_dir_names=None):
        if not os.path.isdir(directory):
            return [], {}
        excluded = excluded_dir_names or []
        result = []
        dirs = {}
        for dirpath, subdirs, filenames in os.walk(directory, followlinks=True, topdown=True):
            subdirs[:] = [d for d in subdirs if d not in excluded]
            for fname in filenames:
                result.append(os.path.relpath(os.path.join(dirpath, fname), directory))
            for d in subdirs:
                dirs[os.path.join(dirpath, d)] = 0.0
        return result, dirs

    def filter_files_extensions(files, extensions):
        return sorted(
            f for f in files
            if os.path.splitext(f)[-1].lower() in extensions or len(extensions) == 0
        )

    fp.add_model_folder_path = add_model_folder_path
    fp.get_folder_paths = get_folder_paths
    fp.recursive_search = recursive_search
    fp.filter_files_extensions = filter_files_extensions
    return fp
