import os
import hashlib
import argparse

# Script 1: Store/append all image hashes from a directory into a file
def store_image_hashes(directory, hash_file):
    with open(hash_file, 'a') as f:
        # Iterate over all files in the directory
        for filename in os.listdir(directory):
            file_path = os.path.join(directory, filename)

            # Skip if it's not a file
            if not os.path.isfile(file_path):
                continue

            # Compute the hash of the image content
            with open(file_path, 'rb') as image_file:
                file_hash = hashlib.md5(image_file.read()).hexdigest()

            # Write the hash to the file
            f.write(f"{file_hash} {filename}\n")

# Script 2: Write a list of files in a specified directory that have hashes in a file
def find_files_with_hashes(directory, hash_file, output_file):
    # Read all hashes from the hash file into a set
    with open(hash_file, 'r') as f:
        known_hashes = set(line.split()[0] for line in f)

    with open(output_file, 'w') as out_f:
        # Iterate over all files in the directory
        for filename in os.listdir(directory):
            file_path = os.path.join(directory, filename)

            # Skip if it's not a file
            if not os.path.isfile(file_path):
                continue

            # Compute the hash of the image content
            with open(file_path, 'rb') as image_file:
                file_hash = hashlib.md5(image_file.read()).hexdigest()

            # Check if the hash is in the known hashes set
            if file_hash in known_hashes:
                out_f.write(f"{filename}\n")

# Main function to handle command line arguments
def main():
    parser = argparse.ArgumentParser(description="Store or find image hashes.")
    subparsers = parser.add_subparsers(dest="command")

    # Subparser for storing image hashes
    store_parser = subparsers.add_parser("store", help="Store image hashes from a directory into a file.")
    store_parser.add_argument("directory", help="Directory containing images.")
    store_parser.add_argument("hash_file", help="File to store image hashes.")

    # Subparser for finding files with hashes
    find_parser = subparsers.add_parser("find", help="Find files in a directory that match hashes in a file.")
    find_parser.add_argument("directory", help="Directory containing images.")
    find_parser.add_argument("hash_file", help="File containing known hashes.")
    find_parser.add_argument("output_file", help="File to store the list of matching files.")

    args = parser.parse_args()

    if args.command == "store":
        store_image_hashes(args.directory, args.hash_file)
    elif args.command == "find":
        find_files_with_hashes(args.directory, args.hash_file, args.output_file)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
