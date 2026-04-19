#!/bin/bash

# Check if correct number of arguments are provided
if [[ $# -ne 2 ]]; then
    echo "Usage: $0 <directory> <file_list>"
    exit 1
fi

# Get command line arguments
directory="$1"
file_list="$2"

# Check if the directory exists
if [[ ! -d "$directory" ]]; then
    echo "Error: Directory '$directory' does not exist."
    exit 1
fi

# Check if the file list exists
if [[ ! -f "$file_list" ]]; then
    echo "Error: File list '$file_list' does not exist."
    exit 1
fi

# Iterate over the file list and delete files in the directory
while IFS= read -r filename; do
    file_path="$directory/$filename"
    if [[ -e "$file_path" ]]; then
        rm "$file_path"
        echo "Deleted: $file_path"
    else
        echo "File not found: $file_path"
    fi
done < "$file_list"

