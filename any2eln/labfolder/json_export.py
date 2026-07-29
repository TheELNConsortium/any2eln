# TheELNConsortium/any2eln
# License MIT

from __future__ import annotations

import csv
import hashlib
import json
import mimetypes
import re
import shutil
import sys
import tempfile
import unicodedata
from collections import defaultdict
from datetime import datetime
from html import escape
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from any2eln.utils.rocrate import get_crate_metadata

JsonObject = dict[str, Any]

LABFOLDER_DATE_FORMATS = (
    '%d.%m.%Y %H:%M',
    '%d.%m.%Y %H:%M:%S',
    '%d.%m.%Y',
)

ASSET_PATH_KEYS = {
    'file',
    'path',
    'filepath',
    'localpath',
    'relativepath',
    'downloadpath',
    'originalpath',
    'originalfile',
    'originaldata',
    'src',
    'href',
    'url',
    'fileurl',
    'imageurl',
    'filename',
    'originalfilename',
    'displayfilename',
    'downloadurl',
    'thumbnailurl',
}

ASSET_NAME_KEYS = {
    'filename',
    'originalfilename',
    'displayfilename',
    'name',
    'title',
}

IGNORED_PROJECT_FILES = {
    'index.html',
    'index.xhtml',
}


class _HtmlAssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key.lower() in {'src', 'href', 'data-src', 'data-href'} and value:
                self.references.append(value)


class LabfolderJson:
    """Convert a Labfolder JSON export into one RO-Crate based .eln archive."""

    def __init__(
        self,
        input_file: str | Path,
        out_dir: str | Path = '.',
        assets_dir: str | Path | None = None,
        category_color: str = '#29aeb9',
        timezone_name: str | None = None,
    ) -> None:
        self.input_file = Path(input_file).expanduser().resolve()
        self.out_dir = Path(out_dir).expanduser().resolve()
        self.assets_dir = Path(assets_dir).expanduser().resolve() if assets_dir else self.input_file.parent
        self.category_color = category_color
        self.timezone = ZoneInfo(timezone_name) if timezone_name else None
        self.projects_dir = self._find_projects_dir()
        self._asset_files: tuple[Path, ...] = ()
        self._project_directories: tuple[Path, ...] = ()
        self._files_by_casefold_name: dict[str, tuple[Path, ...]] = {}
        self._files_by_normalised_name: dict[str, tuple[Path, ...]] = {}

    def extract(self) -> Path:
        data = self._load()
        projects = data.get('projects')
        if not isinstance(projects, list):
            raise ValueError('Expected the top-level JSON object to contain a projects array.')

        self._prepare_asset_index()
        projects_by_id = self._projects_by_id(projects)

        self.out_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
        output = self.out_dir / f'labfolder-json-{now}.eln'

        crate = get_crate_metadata()
        graph: list[JsonObject] = crate['@graph']
        root_node: JsonObject = {
            '@id': './',
            '@type': 'Dataset',
            'name': 'Labfolder JSON export',
            'description': 'Labfolder JSON data converted by any2eln',
            'datePublished': datetime.now().astimezone().isoformat(timespec='seconds'),
            'hasPart': [],
        }
        category_nodes: dict[str, JsonObject] = {}
        author_nodes: dict[str, JsonObject] = {}

        with tempfile.TemporaryDirectory(prefix='any2eln-labfolder-json-') as tmp:
            crate_root = Path(tmp) / 'labfolder-export'
            crate_root.mkdir()

            entry_count = 0
            asset_count = 0
            used_dataset_ids: set[str] = set()
            for project, category_name, project_tag, project_location in self._walk_projects(projects):
                entries = project.get('entries', [])
                if not isinstance(entries, list):
                    self._warn(
                        f"Skipping entries in {project_location} ({project.get('id', '?')}): "
                        'entries is not an array.'
                    )
                    entries = []

                category_id = self._category_id(category_name)
                category_nodes.setdefault(
                    category_id,
                    {
                        '@id': category_id,
                        '@type': 'Thing',
                        'name': category_name,
                        'color': self.category_color,
                    },
                )
                if not entries:
                    continue

                project_directories = self._project_asset_directories(project, projects_by_id)
                project_files = self._files_for_project(project_directories)
                author_id = self._add_project_author_node(project, author_nodes)

                for entry_index, entry in enumerate(entries):
                    if not isinstance(entry, dict):
                        self._warn(
                            f"Skipping entry {entry_index} in {project_location} "
                            f"({project.get('id', '?')}): expected an object."
                        )
                        continue

                    dataset_id, entry_folder_name = self._unique_dataset_id(project, entry, used_dataset_ids)
                    entry_folder = crate_root / entry_folder_name
                    entry_folder.mkdir()

                    body_parts: list[str] = []
                    file_ids: list[str] = []
                    file_nodes: list[JsonObject] = []
                    copied_assets: dict[Path, str] = {}

                    elements = entry.get('elements', [])
                    if not isinstance(elements, list):
                        self._warn(f"Entry {entry.get('id', entry_index)} has a non-array elements value; ignoring it.")
                        elements = []

                    for element_position, element in enumerate(sorted(elements, key=self._element_sort_key)):
                        if not isinstance(element, dict):
                            continue
                        html, nodes = self._process_element(
                            element=element,
                            element_position=element_position,
                            entry=entry,
                            entry_folder=entry_folder,
                            copied_assets=copied_assets,
                            project_directories=project_directories,
                            project_files=project_files,
                        )
                        if html:
                            body_parts.append(html)
                        for node in nodes:
                            file_nodes.append(node)
                            file_ids.append(node['@id'])
                            if node.get('creativeWorkStatus') != 'Archived':
                                asset_count += 1

                    # Keep the complete source object, including project context, as an archived attachment.
                    source_node = self._write_json_attachment(
                        entry_folder,
                        'labfolder-source.json',
                        {'project': {key: value for key, value in project.items() if key != 'entries'}, 'entry': entry},
                        archived=True,
                    )
                    file_nodes.append(source_node)
                    file_ids.append(source_node['@id'])

                    graph.extend(file_nodes)
                    dataset_node = self._dataset_node(
                        dataset_id=dataset_id,
                        entry=entry,
                        category_id=category_id,
                        author_id=author_id,
                        project_tag=project_tag,
                        body_parts=body_parts,
                        file_ids=file_ids,
                    )
                    graph.append(dataset_node)
                    root_node['hasPart'].append({'@id': dataset_id})
                    entry_count += 1

            graph.extend(category_nodes.values())
            graph.extend(author_nodes.values())
            graph.append(root_node)

            with (crate_root / 'ro-crate-metadata.json').open('w', encoding='utf-8') as metadata_file:
                json.dump(crate, metadata_file, ensure_ascii=False, indent=2)

            archive_base = output.with_suffix('')
            zip_path = Path(
                shutil.make_archive(
                    str(archive_base),
                    'zip',
                    root_dir=crate_root.parent,
                    base_dir=crate_root.name,
                )
            )
            zip_path.replace(output)

        print(f'Created {output} with {entry_count} entries and {asset_count} files.')
        return output

    def _find_projects_dir(self) -> Path:
        """Return the Labfolder XHTML export's projects directory."""
        if self.assets_dir.name.casefold() == 'projects':
            return self.assets_dir

        candidates = (
            self.assets_dir / 'projects',
            self.input_file.parent / 'projects',
        )
        for candidate in candidates:
            if candidate.is_dir():
                return candidate.resolve()
        return candidates[0].resolve()

    def _prepare_asset_index(self) -> None:
        """Index project files once so repeated entry lookups stay inexpensive."""
        if not self.projects_dir.is_dir():
            self._warn(
                f'Could not find the Labfolder projects directory at {self.projects_dir}. '
                'The ELN will be created without exported files.'
            )
            return

        files: list[Path] = []
        project_directories: set[Path] = set()
        by_casefold_name: defaultdict[str, list[Path]] = defaultdict(list)
        by_normalised_name: defaultdict[str, list[Path]] = defaultdict(list)

        for candidate in self.projects_dir.rglob('*'):
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if candidate.name.casefold() in IGNORED_PROJECT_FILES:
                project_directories.add(resolved.parent)
                continue
            files.append(resolved)
            by_casefold_name[candidate.name.casefold()].append(resolved)
            by_normalised_name[self._normalise_for_match(candidate.name)].append(resolved)

            # Include all ancestors as matching candidates. Normally index.html marks
            # the project root, but this also supports incomplete or older exports.
            parent = resolved.parent
            while parent != self.projects_dir and parent.is_relative_to(self.projects_dir):
                project_directories.add(parent)
                parent = parent.parent

        self._asset_files = tuple(sorted(files))
        self._project_directories = tuple(sorted(project_directories))
        self._files_by_casefold_name = {
            name: tuple(paths) for name, paths in by_casefold_name.items()
        }
        self._files_by_normalised_name = {
            name: tuple(paths) for name, paths in by_normalised_name.items()
        }

    def _projects_by_id(self, projects: list[Any]) -> dict[str, JsonObject]:
        result: dict[str, JsonObject] = {}

        def walk(nodes: list[Any]) -> None:
            for project in nodes:
                if not isinstance(project, dict):
                    continue
                project_id = self._string(project.get('id')).strip()
                if project_id:
                    result.setdefault(project_id, project)
                children = project.get('children')
                if isinstance(children, list):
                    walk(children)

        walk(projects)
        return result

    def _walk_projects(
        self,
        projects: list[Any],
        category_name: str | None = None,
        location: str = 'projects',
    ) -> Iterable[tuple[JsonObject, str, str | None, str]]:
        """Yield every project in a nested export with its inherited category."""
        for project_index, project in enumerate(projects):
            project_location = f'{location}[{project_index}]'
            if not isinstance(project, dict):
                self._warn(f'Skipping {project_location}: expected an object.')
                continue

            project_name = self._string(project.get('name')).strip()
            current_category = category_name or project_name or 'Uncategorized'
            # A top-level project defines the Experiment Category. Descendants keep
            # that category and contribute their own project name as a tag.
            project_tag = project_name if category_name is not None and project_name else None
            yield project, current_category, project_tag, project_location

            children = project.get('children')
            if children is None:
                continue
            if not isinstance(children, list):
                self._warn(f'Ignoring invalid children value in {project_location}: expected an array.')
                continue
            yield from self._walk_projects(children, current_category, f'{project_location}.children')

    def _project_asset_directories(
        self,
        project: JsonObject,
        projects_by_id: dict[str, JsonObject],
    ) -> tuple[Path, ...]:
        """Find the physical XHTML-export directory matching a JSON project."""
        explicit_directories: list[Path] = []
        for reference in self._project_path_references(project):
            directory = self._resolve_project_directory(reference)
            if directory is not None and directory not in explicit_directories:
                explicit_directories.append(directory)
        if explicit_directories:
            return tuple(explicit_directories)
        if not self._project_directories:
            return ()

        chain = self._project_name_chain(project, projects_by_id)
        chain_normalised = [self._normalise_for_match(part) for part in chain]
        chain_normalised = [part for part in chain_normalised if part]
        project_name = self._normalise_for_match(self._string(project.get('name')))
        project_id = self._normalise_for_match(self._string(project.get('id')))
        group_id = self._normalise_for_match(self._string(project.get('groupId')))

        scored: list[tuple[int, Path]] = []
        for directory in self._project_directories:
            try:
                relative_parts = directory.relative_to(self.projects_dir).parts
            except ValueError:
                continue
            normalised_parts = [self._normalise_for_match(part) for part in relative_parts]
            score = 0

            if chain_normalised and len(normalised_parts) >= len(chain_normalised):
                suffix = normalised_parts[-len(chain_normalised):]
                if suffix == chain_normalised:
                    score = 1000 + len(chain_normalised) * 10
                elif all(
                    self._path_component_matches(actual, expected)
                    for actual, expected in zip(suffix, chain_normalised)
                ):
                    score = 850 + len(chain_normalised) * 10

            directory_name = normalised_parts[-1] if normalised_parts else ''
            if project_name and directory_name == project_name:
                score = max(score, 700)
            elif project_name and self._path_component_matches(directory_name, project_name):
                score = max(score, 600)

            normalised_path = '/'.join(normalised_parts)
            if project_id and project_id in normalised_path:
                score += 100
            if group_id and group_id in normalised_path:
                score += 20

            if score:
                scored.append((score, directory))

        if not scored:
            self._warn(
                f"Could not identify the files directory for Labfolder project "
                f"{project.get('id', '?')} ({project.get('name', 'unnamed')})."
            )
            return ()

        best_score = max(score for score, _ in scored)
        return tuple(directory for score, directory in scored if score == best_score)

    def _project_name_chain(
        self,
        project: JsonObject,
        projects_by_id: dict[str, JsonObject],
    ) -> list[str]:
        chain: list[str] = []
        current = project
        seen: set[str] = set()

        while True:
            current_id = self._string(current.get('id')).strip()
            if current_id:
                if current_id in seen:
                    self._warn(f'Cycle found in Labfolder project hierarchy at project {current_id}.')
                    break
                seen.add(current_id)

            name = self._string(current.get('name')).strip()
            if name:
                chain.append(name)

            parent_id = self._string(current.get('parentId')).strip()
            if not parent_id or parent_id == '0':
                break
            parent = projects_by_id.get(parent_id)
            if parent is None:
                break
            current = parent

        chain.reverse()
        return chain

    def _project_path_references(self, project: JsonObject) -> Iterable[str]:
        path_keys = {'path', 'relativepath', 'exportpath', 'folderpath', 'directory'}
        for key, value in project.items():
            normalised_key = re.sub(r'[^a-z]', '', str(key).lower())
            if normalised_key in path_keys and isinstance(value, str) and value.strip():
                yield value.strip()

    def _resolve_project_directory(self, reference: str) -> Path | None:
        parsed = urlparse(reference)
        if parsed.scheme in {'http', 'https', 'data'}:
            return None
        raw_path = unquote(parsed.path or reference).replace('\\', '/')
        reference_path = Path(raw_path)
        candidates: list[Path] = []
        if reference_path.is_absolute():
            candidates.append(reference_path)
        else:
            candidates.extend((
                self.projects_dir / reference_path,
                self.assets_dir / reference_path,
                self.input_file.parent / reference_path,
            ))
        for candidate in candidates:
            try:
                resolved = candidate.expanduser().resolve()
            except OSError:
                continue
            if resolved.is_file() and resolved.name.casefold() in IGNORED_PROJECT_FILES:
                resolved = resolved.parent
            if resolved.is_dir() and self._is_allowed_asset_path(resolved):
                return resolved
        return None

    def _files_for_project(self, project_directories: tuple[Path, ...]) -> tuple[Path, ...]:
        if not project_directories:
            return self._asset_files
        return tuple(
            asset
            for asset in self._asset_files
            if any(asset.is_relative_to(directory) for directory in project_directories)
        )

    def _load(self) -> JsonObject:
        with self.input_file.open(encoding='utf-8') as source:
            data = json.load(source)
        if not isinstance(data, dict):
            raise ValueError('Expected a top-level JSON object.')
        return data

    def _dataset_node(
        self,
        dataset_id: str,
        entry: JsonObject,
        category_id: str,
        author_id: str | None,
        project_tag: str | None,
        body_parts: list[str],
        file_ids: list[str],
    ) -> JsonObject:
        entry_id = self._string(entry.get('id')).strip()
        title = self._string(entry.get('title')).strip() or f'Untitled Labfolder entry {entry_id or "unknown"}'
        created = self._parse_date(entry.get('created'), f'entry {entry_id} created')
        modified = self._parse_date(entry.get('modified'), f'entry {entry_id} modified')

        node: JsonObject = {
            '@id': dataset_id,
            '@type': 'Dataset',
            'name': title,
            'genre': 'experiment',
            'encodingFormat': 'text/html',
            'text': '\n<hr>\n'.join(body_parts),
            'identifier': f'labfolder-entry:{entry_id}' if entry_id else dataset_id,
            'about': {'@id': category_id},
            #'conditionsOfAccess': 'Locked' if bool(entry.get('readOnly')) else 'Unlocked',
            'hasPart': [{'@id': file_id} for file_id in file_ids],
        }
        if author_id:
            node['author'] = {'@id': author_id}
        if created:
            node['dateCreated'] = created
            # eLabFTW uses temporal as the experiment date during import.
            node['temporal'] = created
        if modified:
            node['dateModified'] = modified

        tags = self._normalise_tags(entry.get('tags'))
        if project_tag and project_tag.casefold() not in {tag.casefold() for tag in tags}:
            tags.append(project_tag)
        if tags:
            # eLabFTW accepts either a comma-separated string or an array here.
            node['keywords'] = tags
        return node

    def _process_element(
        self,
        element: JsonObject,
        element_position: int,
        entry: JsonObject,
        entry_folder: Path,
        copied_assets: dict[Path, str],
        project_directories: tuple[Path, ...],
        project_files: tuple[Path, ...],
    ) -> tuple[str, list[JsonObject]]:
        element_type = self._string(element.get('type')).strip().lower().replace('-', '_')
        content = element.get('content')
        html = ''
        nodes: list[JsonObject] = []

        if isinstance(content, str):
            if element_type == 'text' or self._looks_like_html(content):
                html = content if self._looks_like_html(content) else f'<p>{escape(content)}</p>'

        references = tuple(self._asset_references(element, element_type))
        found_asset = False

        # Files are stored in the XHTML export's sibling projects/ tree. Resolve
        # references relative to the matching project directory before using the
        # export-wide index as a fallback.
        if element_type in {'file', 'image'} or isinstance(content, str):
            for reference in references:
                source = self._resolve_asset(
                    reference=reference,
                    project_directories=project_directories,
                    project_files=project_files,
                    entry=entry,
                    element=element,
                )
                if source is None:
                    continue
                found_asset = True
                if source in copied_assets:
                    continue
                display_name = self._asset_display_name(element, source, element_type)
                destination = self._unique_destination(entry_folder, display_name, element_position)
                shutil.copy2(source, destination)
                node = self._file_node(
                    destination,
                    entry_folder,
                    display_name=display_name,
                    alternate_name=reference,
                    archived=False,
                )
                nodes.append(node)
                copied_assets[source] = node['@id']

        if element_type in {'file', 'image'} and not found_asset:
            entry_id = self._string(entry.get('id')).strip() or 'unknown'
            element_id = self._string(element.get('id')).strip() or str(element.get('index', element_position))
            reference_summary = ', '.join(repr(reference) for reference in references[:3]) or 'no filename or path'
            self._warn(
                f'Could not match {element_type} element {element_id} from entry {entry_id} '
                f'to a file below {self.projects_dir} ({reference_summary}).'
            )

        # API-like TABLE/WELL_PLATE JSON can still be converted sheet-by-sheet to CSV.
        if element_type in {'table', 'well_plate', 'wellplate'}:
            for csv_name, csv_content in self._table_csvs(content):
                destination = self._unique_destination(entry_folder, csv_name, element_position)
                destination.write_text(csv_content, encoding='utf-8')
                nodes.append(self._file_node(destination, entry_folder, archived=False))

        # Preserve every non-text block independently. This is useful while mappings for
        # additional Labfolder export block types are being added.
        if element_type != 'text':
            element_index = element.get('index', element_position)
            raw_name = f'element-{element_index}-{element_type or "unknown"}.json'
            nodes.append(self._write_json_attachment(entry_folder, raw_name, element, archived=True))

        return html, nodes

    def _write_json_attachment(
        self,
        entry_folder: Path,
        filename: str,
        payload: Any,
        archived: bool,
    ) -> JsonObject:
        destination = self._unique_destination(entry_folder, filename)
        destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return self._file_node(destination, entry_folder, archived=archived)

    def _file_node(
        self,
        file_path: Path,
        entry_folder: Path,
        display_name: str | None = None,
        alternate_name: str | None = None,
        archived: bool = False,
    ) -> JsonObject:
        data = file_path.read_bytes()
        node: JsonObject = {
            '@id': f'./{entry_folder.name}/{file_path.name}',
            '@type': 'File',
            'name': self._safe_component(Path(display_name).name) if display_name else file_path.name,
            'encodingFormat': mimetypes.guess_type(file_path.name)[0] or 'application/octet-stream',
            'contentSize': len(data),
            'sha256': hashlib.sha256(data).hexdigest(),
        }
        if alternate_name:
            node['alternateName'] = alternate_name
        if archived:
            node['creativeWorkStatus'] = 'Archived'
        return node

    def _table_csvs(self, content: Any) -> list[tuple[str, str]]:
        if not isinstance(content, dict):
            return []
        sheets = content.get('sheets')
        if not isinstance(sheets, dict):
            return []

        result: list[tuple[str, str]] = []
        for sheet_name, sheet in sheets.items():
            if not isinstance(sheet, dict):
                continue
            data = sheet.get('data')
            table = data.get('dataTable') if isinstance(data, dict) else None
            if not isinstance(table, dict):
                continue

            rows: list[dict[str, Any]] = []
            columns: set[str] = set()
            for row_value in table.values():
                if not isinstance(row_value, dict):
                    continue
                row: dict[str, Any] = {}
                for column, cell in row_value.items():
                    value = cell.get('value', '') if isinstance(cell, dict) else cell
                    row[str(column)] = value
                    columns.add(str(column))
                rows.append(row)

            if not rows:
                continue
            ordered_columns = sorted(columns, key=self._natural_sort_key)
            stream = StringIO(newline='')
            writer = csv.DictWriter(stream, fieldnames=ordered_columns)
            writer.writeheader()
            writer.writerows(rows)
            result.append((f'{self._safe_component(str(sheet_name))}.csv', stream.getvalue()))
        return result

    def _asset_references(self, element: JsonObject, element_type: str) -> Iterable[str]:
        found: list[str] = []
        content = element.get('content')

        # Prefer references found in the HTML body. If eLabFTW rewrites an old
        # attachment name during import, this is the value that actually occurs in
        # the body.
        if isinstance(content, str) and self._looks_like_html(content):
            parser = _HtmlAssetParser()
            parser.feed(content)
            found.extend(parser.references)

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    normalised_key = re.sub(r'[^a-z]', '', str(key).lower())
                    if normalised_key in ASSET_PATH_KEYS and isinstance(nested, str):
                        found.append(nested)
                    walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    walk(nested)

        walk(element)

        if element_type in {'file', 'image'}:
            if isinstance(content, str) and not self._looks_like_html(content):
                found.append(content)
            found.extend(self._asset_name_candidates(element))
            found.extend(self._asset_id_references(element))

        # Preserve order but avoid duplicate references.
        return dict.fromkeys(reference.strip() for reference in found if reference.strip()).keys()

    def _asset_name_candidates(self, element: JsonObject) -> list[str]:
        found: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    normalised_key = re.sub(r'[^a-z]', '', str(key).lower())
                    if (
                        normalised_key in ASSET_NAME_KEYS
                        and isinstance(nested, str)
                        and 0 < len(nested.strip()) <= 1024
                    ):
                        found.append(nested.strip())
                    walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    walk(nested)

        walk(element)
        return list(dict.fromkeys(found))

    def _asset_id_references(self, element: JsonObject) -> list[str]:
        found: list[str] = []
        identity_keys = {'id', 'fileid', 'imageid', 'versionid', 'uuid', 'blockid', 'elementid'}

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    normalised_key = re.sub(r'[^a-z]', '', str(key).lower())
                    if normalised_key in identity_keys and isinstance(nested, (str, int)):
                        found.append(str(nested))
                    walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    walk(nested)

        walk(element)
        return list(dict.fromkeys(found))

    def _resolve_asset(
        self,
        reference: str,
        project_directories: tuple[Path, ...],
        project_files: tuple[Path, ...],
        entry: JsonObject,
        element: JsonObject,
    ) -> Path | None:
        parsed = urlparse(reference)
        if parsed.scheme in {'http', 'https', 'data'}:
            return None
        if not parsed.path and parsed.fragment:
            return None

        raw_path = unquote(parsed.path or reference).replace('\\', '/').strip()
        if not raw_path:
            return None
        reference_path = Path(raw_path)
        element_type = self._string(element.get('type')).strip().lower().replace('-', '_')
        if (
            element_type == 'text'
            and reference_path.suffix.casefold() in {'.html', '.xhtml', '.css', '.js'}
        ):
            return None

        # A path in an XHTML project index is normally relative to that project's
        # directory. Only return immediately when it resolves in exactly one matched
        # project; duplicate project names must not silently select the first file.
        project_matches: set[Path] = set()
        for directory in project_directories:
            for candidate in (directory / reference_path, directory / reference_path.name):
                try:
                    resolved = candidate.expanduser().resolve()
                except OSError:
                    continue
                if resolved.is_file() and self._is_allowed_asset_path(resolved):
                    project_matches.add(resolved)
        if project_matches:
            matches = sorted(project_matches)
            if len(matches) > 1:
                selected = matches[0]
                displayed_matches = ', '.join(
                    str(path.relative_to(self.projects_dir)) for path in matches[:3]
                )
                self._warn(
                    f'Ambiguous Labfolder asset reference {str(reference_path)!r}; '
                    f'matched {displayed_matches}. Using '
                    f'{selected.relative_to(self.projects_dir)}.'
                )
                return selected
            return matches[0]

        candidates: list[Path] = []
        if reference_path.is_absolute():
            candidates.append(reference_path)
            candidates.append(self.assets_dir / raw_path.lstrip('/'))
        else:
            candidates.extend((
                self.projects_dir / reference_path,
                self.assets_dir / reference_path,
                self.input_file.parent / reference_path,
            ))
            if reference_path.parts and reference_path.parts[0].casefold() == 'projects':
                candidates.append(self.projects_dir.joinpath(*reference_path.parts[1:]))

        for candidate in dict.fromkeys(candidates):
            try:
                resolved = candidate.expanduser().resolve()
            except OSError:
                continue
            if resolved.is_file() and self._is_allowed_asset_path(resolved):
                return resolved

        return self._best_asset_match(
            reference_path=reference_path,
            project_files=project_files,
            entry=entry,
            element=element,
        )

    def _best_asset_match(
        self,
        reference_path: Path,
        project_files: tuple[Path, ...],
        entry: JsonObject,
        element: JsonObject,
    ) -> Path | None:
        pool = set(project_files or self._asset_files)
        if not pool:
            return None

        reference_name = reference_path.name
        if not reference_name:
            return None
        reference_casefold = reference_name.casefold()
        reference_normalised = self._normalise_for_match(reference_name)
        reference_stem = self._normalise_for_match(reference_path.stem)
        reference_parts = [
            self._normalise_for_match(part)
            for part in reference_path.parts
            if part not in {'', '.', '..'}
        ]
        reference_parts = [part for part in reference_parts if part]
        identity_tokens = self._asset_identity_tokens(entry, element)

        candidates: set[Path] = set()
        candidates.update(path for path in self._files_by_casefold_name.get(reference_casefold, ()) if path in pool)
        candidates.update(
            path
            for path in self._files_by_normalised_name.get(reference_normalised, ())
            if path in pool
        )

        # Handle XHTML exports that prefix the original name with an element ID,
        # and paths that retain only a suffix of the original relative path.
        for asset in pool:
            asset_name_normalised = self._normalise_for_match(asset.name)
            if reference_normalised and asset_name_normalised.endswith(reference_normalised):
                candidates.add(asset)
                continue
            if reference_stem and self._normalise_for_match(asset.stem).endswith(reference_stem):
                candidates.add(asset)
                continue
            try:
                asset_parts = [
                    self._normalise_for_match(part)
                    for part in asset.relative_to(self.projects_dir).parts
                ]
            except ValueError:
                asset_parts = [self._normalise_for_match(part) for part in asset.parts]
            if reference_parts and len(asset_parts) >= len(reference_parts):
                if asset_parts[-len(reference_parts):] == reference_parts:
                    candidates.add(asset)
                    continue
            normalised_asset_path = '/'.join(asset_parts)
            if identity_tokens and any(token in normalised_asset_path for token in identity_tokens):
                candidates.add(asset)

        scored: list[tuple[int, Path]] = []
        for asset in candidates:
            score = 0
            asset_casefold = asset.name.casefold()
            asset_normalised = self._normalise_for_match(asset.name)
            asset_stem = self._normalise_for_match(asset.stem)
            try:
                asset_parts = [
                    self._normalise_for_match(part)
                    for part in asset.relative_to(self.projects_dir).parts
                ]
            except ValueError:
                asset_parts = [self._normalise_for_match(part) for part in asset.parts]

            if reference_parts and len(asset_parts) >= len(reference_parts):
                if asset_parts[-len(reference_parts):] == reference_parts:
                    score += 1200 + len(reference_parts) * 10
            if asset_casefold == reference_casefold:
                score += 800
            elif reference_normalised and asset_normalised == reference_normalised:
                score += 750
            elif reference_normalised and asset_normalised.endswith(reference_normalised):
                score += 450
            elif reference_stem and asset_stem.endswith(reference_stem):
                score += 400

            normalised_asset_path = '/'.join(asset_parts)
            score += sum(
                weight
                for token, weight in identity_tokens.items()
                if token in normalised_asset_path
            )
            if score:
                scored.append((score, asset))

        if not scored:
            return None

        best_score = max(score for score, _ in scored)
        best = sorted(asset for score, asset in scored if score == best_score)
        if len(best) > 1:
            selected = best[0]
            matches = ', '.join(str(path.relative_to(self.projects_dir)) for path in best[:3])
            self._warn(
                f'Ambiguous Labfolder asset reference {str(reference_path)!r}; '
                f'matched {matches}. Using {selected.relative_to(self.projects_dir)}.'
            )
            return selected
        return best[0]

    def _asset_identity_tokens(self, entry: JsonObject, element: JsonObject) -> dict[str, int]:
        result: dict[str, int] = {}
        identity_keys = {'id', 'fileid', 'imageid', 'versionid', 'uuid', 'blockid', 'elementid'}

        def add(value: Any, weight: int) -> None:
            if not isinstance(value, (str, int)):
                return
            token = self._normalise_for_match(str(value))
            if len(token) >= 3:
                result[token] = max(result.get(token, 0), weight)

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    normalised_key = re.sub(r'[^a-z]', '', str(key).lower())
                    if normalised_key in identity_keys:
                        add(nested, 100)
                    walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    walk(nested)

        walk(element)
        for key in ('id', 'versionId', 'blockNumber'):
            add(entry.get(key), 20)
        return result

    def _is_allowed_asset_path(self, path: Path) -> bool:
        resolved = path.resolve()
        roots = {
            self.assets_dir.resolve(),
            self.input_file.parent.resolve(),
            self.projects_dir.resolve(),
        }
        return any(resolved == root or resolved.is_relative_to(root) for root in roots)

    def _asset_display_name(self, element: JsonObject, source: Path, element_type: str) -> str:
        candidates = self._asset_name_candidates(element)
        display_name = ''
        for candidate in candidates:
            raw_name = unquote(urlparse(candidate).path or candidate).replace('\\', '/')
            name = Path(raw_name).name
            if name and Path(name).suffix:
                display_name = name
                break
        if not display_name:
            for candidate in candidates:
                raw_name = unquote(urlparse(candidate).path or candidate).replace('\\', '/')
                name = Path(raw_name).name
                if name:
                    display_name = name
                    break
        if not display_name:
            display_name = source.name

        if element_type == 'image' and not Path(display_name).suffix:
            extension = self._detect_image_extension(source)
            if extension:
                display_name += extension
        return display_name

    @staticmethod
    def _detect_image_extension(source: Path) -> str | None:
        """Detect image formats supported by eLabFTW thumbnails from magic bytes."""
        try:
            with source.open('rb') as image_file:
                signature = image_file.read(16)
        except OSError:
            return None

        if signature.startswith(b'\x89PNG\r\n\x1a\n'):
            return '.png'
        if signature.startswith(b'\xff\xd8\xff'):
            return '.jpg'
        if signature.startswith((b'GIF87a', b'GIF89a')):
            return '.gif'
        if signature.startswith(b'BM'):
            return '.bmp'
        return None

    def _add_project_author_node(
        self,
        project: JsonObject,
        author_nodes: dict[str, JsonObject],
    ) -> str | None:
        """Create the Person node used by TrustedEln for project ownership."""
        owner = project.get('owner')
        project_id = self._string(project.get('id')).strip() or 'unknown'
        project_name = self._string(project.get('name')).strip() or 'unnamed'
        project_label = f'{project_id} ({project_name})'

        if not isinstance(owner, dict):
            self._warn(
                f'Labfolder project {project_label} has no owner object; its entries will be assigned '
                'to the user running the eLabFTW import.'
            )
            return None

        email = self._string(owner.get('email')).strip()
        if not email:
            self._warn(
                f'Labfolder project {project_label} has an owner without an email address; its entries '
                'will be assigned to the user running the eLabFTW import.'
            )
            return None

        owner_id = self._string(owner.get('id')).strip()
        given_name = self._string(owner.get('firstName')).strip() or 'Unknown'
        family_name = self._string(owner.get('lastName')).strip() or 'Unknown'

        # TrustedEln resolves users by email. Use the normalised email as the stable
        # identity so an owner shared by several projects produces one Person node.
        # The URI shape follows the Person IDs generated by eLabFTW exports.
        digest = hashlib.sha256(email.casefold().encode()).hexdigest()
        author_id = f'person://{digest}?hash_algo=sha256'
        node: JsonObject = {
            '@id': author_id,
            '@type': 'Person',
            'givenName': given_name,
            'familyName': family_name,
            'email': email,
            'name': f'{given_name} {family_name}'.strip(),
        }
        if owner_id:
            node['identifier'] = f'labfolder-user:{owner_id}'

        author_nodes.setdefault(author_id, node)
        return author_id

    def _parse_date(self, value: Any, label: str) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        value = value.strip()
        parsed: datetime | None = None
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            for date_format in LABFOLDER_DATE_FORMATS:
                try:
                    parsed = datetime.strptime(value, date_format)
                    break
                except ValueError:
                    continue
        if parsed is None:
            self._warn(f'Could not parse {label}: {value!r}; omitting the date from the ELN metadata.')
            return None
        if parsed.tzinfo is None and self.timezone is not None:
            parsed = parsed.replace(tzinfo=self.timezone)
        return parsed.isoformat(timespec='seconds')

    def _unique_dataset_id(
        self,
        project: JsonObject,
        entry: JsonObject,
        used_dataset_ids: set[str],
    ) -> tuple[str, str]:
        project_id = self._safe_component(self._string(project.get('id')) or 'unknown-project')
        entry_id = self._safe_component(self._string(entry.get('id')) or 'unknown-entry')
        base = f'project-{project_id}-entry-{entry_id}'
        candidate = base
        suffix = 2
        while f'./{candidate}' in used_dataset_ids:
            candidate = f'{base}-{suffix}'
            suffix += 1
        dataset_id = f'./{candidate}'
        used_dataset_ids.add(dataset_id)
        return dataset_id, candidate

    def _category_id(self, project_name: str) -> str:
        digest = hashlib.sha256(project_name.encode()).hexdigest()[:16]
        return f'#category-{digest}'

    def _normalise_tags(self, tags: Any) -> list[str]:
        if not isinstance(tags, list):
            return []
        result: list[str] = []
        for tag in tags:
            if isinstance(tag, str):
                value = tag.strip()
            elif isinstance(tag, dict):
                value = ''
                for key in ('name', 'title', 'value', 'tag'):
                    candidate = tag.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        value = candidate.strip()
                        break
            else:
                value = ''
            if value and value not in result:
                result.append(value)
        return result

    def _unique_destination(self, directory: Path, filename: str, prefix: int | None = None) -> Path:
        safe_name = self._safe_component(Path(filename).name)
        if prefix is not None:
            safe_name = f'{prefix:04d}-{safe_name}'
        destination = directory / safe_name
        stem = destination.stem
        suffix = destination.suffix
        number = 2
        while destination.exists():
            destination = directory / f'{stem}-{number}{suffix}'
            number += 1
        return destination

    @staticmethod
    def _element_sort_key(element: Any) -> tuple[int, str]:
        if not isinstance(element, dict):
            return (sys.maxsize, '')
        raw_index = element.get('index', sys.maxsize)
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = sys.maxsize
        return index, str(element.get('type', ''))

    @staticmethod
    def _natural_sort_key(value: str) -> tuple[int, str]:
        try:
            return int(value), value
        except ValueError:
            return sys.maxsize, value

    @staticmethod
    def _looks_like_html(value: str) -> bool:
        return bool(re.search(r'<[a-zA-Z][^>]*>', value))

    @staticmethod
    def _normalise_for_match(value: str) -> str:
        value = unicodedata.normalize('NFKD', unquote(value)).casefold()
        return ''.join(character for character in value if character.isalnum())

    @staticmethod
    def _path_component_matches(actual: str, expected: str) -> bool:
        if actual == expected:
            return True
        if len(expected) < 5:
            return False
        return actual.startswith(expected) or actual.endswith(expected)

    @staticmethod
    def _safe_component(value: str) -> str:
        value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '_', value.strip())
        value = re.sub(r'\s+', ' ', value).strip(' .')
        return value or 'unnamed'

    @staticmethod
    def _string(value: Any) -> str:
        return value if isinstance(value, str) else '' if value is None else str(value)

    @staticmethod
    def _warn(message: str) -> None:
        print(f'Warning: {message}', file=sys.stderr)
