# TheELNConsortium/any2eln
# © 2024 Nicolas CARPi @ Deltablot
# License MIT

from datetime import datetime
from typing import Any

def get_crate_metadata() -> dict[str, Any]:
    crate_metadata: dict[str, Any] = {}
    crate_metadata['@context'] = 'https://w3id.org/ro/crate/1.1/context'
    crate_metadata['@graph'] = []

    crate_node: dict[str, Any] = {}
    crate_node['@id'] = 'ro-crate-metadata.json'
    crate_node['@type'] = 'CreativeWork'
    crate_node['about'] = {'@id': './'}
    crate_node['conformsTo'] = {'@id': 'https://w3id.org/ro/crate/1.1'}
    crate_node['dateCreated'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S%z')
    crate_node['version'] = '1.0'

    crate_metadata['@graph'].append(crate_node)
    return crate_metadata
