import xml.etree.ElementTree as ET

def read_email_config(xml_path):
    """
    Parses an XML file and returns a dictionary of email configuration.

    Expected keys:
    - email_host
    - email_from
    - email_to
    """
    config = {}
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        for add in root.findall(".//add"):
            key = add.attrib.get("key")
            value = add.attrib.get("value")
            if key and value:
                config[key] = value.strip()
    except Exception as e:
        print(f"Error reading email config: {e}")
    return config