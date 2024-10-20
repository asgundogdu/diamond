import os
import json
from pathlib import Path

def find_huggingface_cache():
    possible_cache_dirs = [
        Path.home() / '.cache' / 'huggingface' / 'datasets',
        Path.home() / 'Library' / 'Caches' / 'huggingface' / 'datasets',  # for macOS
    ]
    for cache_dir in possible_cache_dirs:
        if cache_dir.exists():
            return cache_dir
    return None

def explore_atari_dataset():
    cache_dir = find_huggingface_cache()
    if not cache_dir:
        print("Hugging Face cache directory not found.")
        return

    print(f"Exploring Hugging Face cache directory: {cache_dir}")

    # Look for Atari-related directories
    atari_dirs = list(cache_dir.glob('*atari*'))
    if not atari_dirs:
        print("No Atari-related directories found in the cache.")
        return

    for atari_dir in atari_dirs:
        print(f"\nExploring Atari dataset at: {atari_dir}")
        
        # List all files/directories in the dataset path
        contents = os.listdir(atari_dir)
        print(f"Contents of the dataset directory: {contents}")

        # Look for dataset files
        dataset_files = [f for f in contents if f.endswith('.arrow') or f.endswith('.parquet')]
        if dataset_files:
            print(f"Dataset files found: {dataset_files}")
            # You might need additional libraries to read these files
            # For example, pyarrow for .arrow files or pandas for .parquet files
        else:
            print("No .arrow or .parquet files found in the dataset directory.")

        # Look for metadata files
        metadata_files = [f for f in contents if f.endswith('.json')]
        for metadata_file in metadata_files:
            print(f"\nReading metadata from: {metadata_file}")
            with open(atari_dir / metadata_file, 'r') as f:
                metadata = json.load(f)
            print("Metadata:")
            print(json.dumps(metadata, indent=2))

    print("\nNote: To explore the actual data, you might need to use the Hugging Face datasets library or specific tools for reading .arrow or .parquet files.")

if __name__ == "__main__":
    explore_atari_dataset()