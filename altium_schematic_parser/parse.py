import argparse
import textwrap
import olefile
import re
import json
import math
import logging

# Set up logging configuration
logging.basicConfig()
lg = logging.getLogger(__name__)
# Compile regex patterns once for device name matching
device_name_pattern = re.compile(r'^(?P<prefix>X)(?P<index>\d+)$')


def parse(input, format, **kwargs):
    """Parse the input .SchDoc file and return the schematic in the specified format."""
    full_path = input
    ole_file = olefile.OleFileIO(full_path)

    # Open the 'FileHeader' stream and read its content
    stream = ole_file.openstream('FileHeader')
    pattern = re.compile(b'.{3}\x00\x00\\|')
    lines = pattern.split(stream.read()[5:-1])

    schematic = {}
    datums = []

    # Process each line to extract key-value pairs
    for line in lines:
        datum = {}
        pairs = line.split(b"|")
        for pair in pairs:
            data = pair.split(b"=")
            if len(data) >= 2:
                # Decode both key and value
                datum[data[0].decode()] = data[1].decode('utf-8', 'ignore')
            elif len(data) == 1 and data[0]:
                datum[data[0].decode()] = ""
        datums.append(datum)

    # Organize datums into header and records
    schematic["header"] = [x for x in datums if 'HEADER' in x]
    schematic["records"] = [x for x in datums if 'RECORD' in x]

    # Determine the hierarchy of the schematic
    hierarchy_schematic = determine_hierarchy(schematic)

    # Format the schematic based on the requested output format
    if format == 'all-hierarchy':
        schematic = hierarchy_schematic
    elif format == 'parts-list':
        schematic = determine_parts_list(hierarchy_schematic)
    elif format == 'net-list':
        schematic = determine_net_list(hierarchy_schematic)

    return schematic


def determine_hierarchy(schematic):
    """Convert a flat list of records into a hierarchical structure."""
    records_copy = schematic["records"]
    schematic["hierarchy"] = []

    for i, current in enumerate(records_copy):
        current['index'] = i
        owner_index = current.get("OWNERINDEX")

        if owner_index is None:
            schematic["hierarchy"].append(current)
        else:
            try:
                owner_index = int(owner_index)
                if 0 <= owner_index < len(records_copy):
                    owner = records_copy[owner_index]
                    if "children" not in owner:
                        owner["children"] = []
                    owner["children"].append(current)
                else:
                    schematic["hierarchy"].append(current)
            except (ValueError, TypeError):
                schematic["hierarchy"].append(current)

    # Update records to the hierarchical structure and remove temporary hierarchy
    schematic["records"] = schematic["hierarchy"]
    schematic.pop("hierarchy", None)
    return schematic


def determine_parts_list(schematic):
    """Extract a list of parts from the schematic."""
    parts_list = {
        "records": [record for record in schematic["records"] if record.get("RECORD") == "1"]
    }
    return parts_list


def determine_net_list(schematic):
    """Determine the net list from the schematic."""
    devices = []

    # Find devices of specific record types
    for record_type in ["27", "2", "25", "17"]:
        _, found_devices = find_record(schematic, key="RECORD", value=record_type)
        devices.extend(found_devices)

    # Calculate coordinates for devices
    for device in devices:
        if device["RECORD"] == "2":
            if all(key in device for key in ["PINCONGLOMERATE", "LOCATION.X", "LOCATION.Y", "PINLENGTH"]):
                rotation = (int(device["PINCONGLOMERATE"]) & 0x03) * 90
                device['coords'] = [[
                    int(int(device['LOCATION.X']) + math.cos(rotation / 180 * math.pi) * int(device['PINLENGTH'])),
                    int(int(device['LOCATION.Y']) + math.sin(rotation / 180 * math.pi) * int(device['PINLENGTH']))
                ]]
            else:
                device['coords'] = [(0, 0)]
        elif device["RECORD"] == "27":
            coord_name_matches = [device_name_pattern.match(key) for key in device.keys()]
            device['coords'] = []
            for match in filter(None, coord_name_matches):
                x_key = 'X' + match.group('index')
                y_key = 'Y' + match.group('index')
                if x_key in device and y_key in device:
                    device['coords'].append((int(device[x_key]), int(device[y_key])))
            if not device['coords']:
                device['coords'] = [(0, 0)]
        else:
            if "LOCATION.X" in device and "LOCATION.Y" in device:
                device['coords'] = [(int(device['LOCATION.X']), int(device['LOCATION.Y']))]
            else:
                device['coords'] = [(0, 0)]

    # Create nets from connected devices
    nets = []
    visited_indices = set()
    for device in devices:
        if device["index"] not in visited_indices:
            visited_indices.add(device["index"])
            net = {
                'name': None,
                'devices': find_connected_wires(device, devices, [], schematic)
            }
            nets.append(net)

    # Assign names to nets if not already named
    for net in nets:
        net['devices'].sort(key=lambda k: k['index'])
        if not net['name']:
            net['name'] = next((
                d['TEXT'] for d in net['devices'] if (d['RECORD'] in ['17', '25']) and 'TEXT' in d), None
            )
        if not net['name']:
            naming_pin = next((d for d in net['devices'] if d['RECORD'] == '2'), None)
            if naming_pin and 'OWNERINDEX' in naming_pin:
                try:
                    parent_results = find_record(schematic, key="index", value=int(naming_pin['OWNERINDEX']))
                    parent = next(iter(parent_results[1]), None) if parent_results[1] else None
                    if parent and 'children' in parent:
                        net['name'] = next((
                            'Net' + r['TEXT'] for r in parent['children'] if (r['RECORD'] == '34' and 'TEXT' in r)
                        ), None)
                except (ValueError, TypeError):
                    pass

    schematic["nets"] = nets
    return schematic


def find_record(schematic, key, value, record=None, visited=None, found=None):
    """Find records in the schematic that match the given key-value pair."""
    lg.debug("Finding records where: {0} = {1}".format(key, value))

    if visited is None:
        visited = []
    if found is None:
        found = []
    if record is None:
        for record in schematic['records']:
            visited, found = find_record(schematic, key, value, record=record, visited=visited, found=found)
    else:
        if record['index'] not in {r['index'] for r in visited}:
            visited.append(record)
            if key in record and record[key] == value:
                found.append(record)
            if "children" in record:
                for child_record in record["children"]:
                    visited, found = find_record(schematic, key, value, record=child_record, visited=visited,
                                                 found=found)
    return visited, found


def find_connected_wires(wire, devices, visited, schematic):
    """Find all wires connected to the given wire."""
    neighbors = find_neighbors(wire, devices, schematic)
    lg.debug('Entering: {0}'.format(wire['index']))

    if wire['index'] not in {w['index'] for w in visited}:
        lg.debug('Adding: {0} to {1}'.format(wire['index'], [w['index'] for w in visited]))
        visited.append(wire)
        for neighbor in neighbors:
            lg.debug('Trying: {0} of {1}'.format(neighbor['index'], [x['index'] for x in neighbors]))
            visited = find_connected_wires(neighbor, devices, visited, schematic)
            lg.debug('Visited = {0}'.format([w['index'] for w in visited]))
    else:
        lg.debug('Skipping: {0} already in list {1}'.format(wire['index'], [w['index'] for w in visited]))

    lg.debug('Returning: {0}'.format(wire['index']))
    return visited


def find_neighbors(wire, devices, schematic):
    """Find neighboring wires connected to the given wire."""
    return [other_wire for other_wire in devices if other_wire != wire and is_connected(wire, other_wire)]


def is_connected(wire_a, wire_b):
    """Check if two wires are connected."""
    if 'coords' not in wire_a or 'coords' not in wire_b:
        return False

    # Create line segments for each wire
    a_line_segments = [(wire_a['coords'][i], wire_a['coords'][i + 1]) for i in range(len(wire_a['coords']) - 1)] if len(
        wire_a['coords']) > 1 else [(wire_a['coords'][0], wire_a['coords'][0])]
    b_line_segments = [(wire_b['coords'][i], wire_b['coords'][i + 1]) for i in range(len(wire_b['coords']) - 1)] if len(
        wire_b['coords']) > 1 else [(wire_b['coords'][0], wire_b['coords'][0])]

    # Check for intersection between line segments
    for vertex in [vx for line in a_line_segments for vx in line]:
        for b_line in b_line_segments:
            b_xs = sorted(list(zip(*b_line))[0])
            b_ys = sorted(list(zip(*b_line))[1])
            if ((min(b_xs) <= vertex[0] <= max(b_xs)) and (min(b_ys) <= vertex[1] <= max(b_ys))):
                return True

    for vertex in [vx for line in b_line_segments for vx in line]:
        for a_line in a_line_segments:
            a_xs = sorted(list(zip(*a_line))[0])
            a_ys = sorted(list(zip(*a_line))[1])
            if ((min(a_xs) <= vertex[0] <= max(a_xs)) and (min(a_ys) <= vertex[1] <= max(a_ys))):
                return True

    # Special case for wires with the same TEXT
    if (wire_a["RECORD"] == "17") and (wire_b["RECORD"] == "17") and (
            'TEXT' in wire_a and 'TEXT' in wire_b and wire_a["TEXT"] == wire_b["TEXT"]):
        return True

    return False


def main(args):
    """Main function to parse arguments and execute the schematic parsing."""
    try:
        schematic = parse(**vars(args))
        if args.output:
            # Write the schematic to a JSON file if output path is provided
            with open(args.output, 'w') as json_file:
                json.dump(schematic, json_file, indent=4)
        else:
            print(json.dumps(schematic, indent=4))  # Print the schematic to the terminal
    except Exception as e:
        print(f"Error processing file: {e}")
        return 1
    return 0


if __name__ == "__main__":
    # Set up argument parser
    parser = argparse.ArgumentParser(description='Converts Altium .SchDoc files into JSON.',
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('input',
                        help='path/to/altiumschematic.schdoc file to parse')
    parser.add_argument('-o', '--output', dest='output',
                        help='path/to/jsonfile.json file to output json to, otherwise prints to terminal')
    parser.add_argument('-f', '--format', dest='format', default='all-hierarchy',
                        choices=['all-list', 'all-hierarchy', 'parts-list', 'net-list'],
                        help=textwrap.dedent('''\
                        all-list: All records in a flattened list
                        all-hierarchy: All records in an owner/child hierarchy
                        parts-list: A listing of parts and their designators
                        net-list: A listing of nets between parts pins, referred to by their designators'''))

    args = parser.parse_args()
    main(args)