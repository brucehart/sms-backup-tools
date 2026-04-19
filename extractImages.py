import os
import xml.etree.ElementTree as ET
import base64
import datetime
import shutil
import argparse
import uuid

def save_images_from_xml(xml_file_path, output_directory):
    # Create output directory if it doesn't exist
    if not os.path.exists(output_directory):
        os.makedirs(output_directory)

    # Define image content types and their corresponding extensions
    image_content_types = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/gif": ".gif"
    }

    # Stream parse the XML file
    try: 
       context = ET.iterparse(xml_file_path, events=("start", "end"))
    except ET.ParseError as e:
       print(f"ParseError: {e}")
       return
       
    total_parts = 0
    processed_parts = 0

    # First pass to count total parts
    for event, elem in context:
        if event == "end" and elem.tag == "part" and elem.attrib.get("ct") in image_content_types:
            total_parts += 1
        elem.clear()
    del context

    # Stream parse again to process images
    context = ET.iterparse(xml_file_path, events=("start", "end"))
    current_mms_date = None
    for event, elem in context:
        if event == "end":
            if elem.tag == "mms":
                # Get the date attribute from the MMS element
                current_mms_date = elem.attrib.get("date")

            elif elem.tag == "part" and elem.attrib.get("ct") in image_content_types:
                # Extract image data and save it to file
                content_type = elem.attrib["ct"]
                data = elem.attrib.get("data")

                if data:
                    # Decode base64 data
                    image_data = base64.b64decode(data)

                    # Convert date field to datetime
                    if current_mms_date:
                        date_timestamp = int(current_mms_date) / 1000  # Assuming timestamp is in milliseconds
                        date_time = datetime.datetime.fromtimestamp(date_timestamp)
                        date_str = date_time.strftime("%Y-%m-%d-%H-%M-%S")
                    else:
                        date_str = "unknown_date"

                    # Define file name and path with a unique identifier to avoid overwriting
                    file_extension = image_content_types[content_type]
                    unique_id = uuid.uuid4().hex[:8]
                    file_name = f"{date_str}_{unique_id}{file_extension}"
                    file_path = os.path.join(output_directory, file_name)

                    # Write image to file
                    with open(file_path, "wb") as image_file:
                        image_file.write(image_data)

                    # Set file metadata to date_field (only on Unix systems)
                    if current_mms_date:
                        os.utime(file_path, (date_time.timestamp(), date_time.timestamp()))

                # Update progress
                processed_parts += 1
                print(f"Progress: {processed_parts}/{total_parts} parts processed")

                # Clear the element to free up memory
                elem.clear()

    # Clean up XML parser
    del context

def main():
    parser = argparse.ArgumentParser(description="Save images from an XML file.")
    parser.add_argument("xml_file_path", help="Path to the input XML file.")
    parser.add_argument("output_directory", help="Directory to save the extracted images.")
    args = parser.parse_args()

    save_images_from_xml(args.xml_file_path, args.output_directory)

if __name__ == "__main__":
    main()
