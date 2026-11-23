from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from forge.context.workspace import Workspace
from forge.intelligence.models import DependencyRecord, ExtractedFileSymbols, ScannedFile
from forge.intelligence.parsers.registry import get_parser_for_language
from forge.intelligence.scanner import RepoScanner
from forge.intelligence.store import IndexStats, IndexStore

logger = logging.getLogger("forge.intelligence.indexer")


class RepositoryIndexer:
    """Orchestrates bounded, incremental, transactional repository indexing into SQLite."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        store: IndexStore | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.store = store or IndexStore(self.workspace_root)
        self.workspace = workspace or Workspace(self.workspace_root)
        self.scanner = RepoScanner(self.workspace_root, workspace=self.workspace)

    def index(self, *, force: bool = False) -> IndexStats:
        """Run incremental or forced indexing across the workspace."""
        start_time = time.perf_counter()
        run_id = self.store.start_run()
        files_indexed = 0
        files_removed = 0

        try:
            if force:
                logger.info("Forced re-indexing requested; clearing previous index data.")
                self.store.clear_index()

            # 1. Bounded scan of workspace
            inventory = self.scanner.scan()
            existing_files = self.store.get_existing_files() if not force else {}

            scanned_map = {f.relative_path: f for f in inventory.files}

            # 2. Identify additions, modifications, deletions, and renames
            to_index: list[ScannedFile] = []
            removed_paths: list[str] = []

            # If the scan was truncated, we do not have a complete view of the workspace.
            # Avoid deletion reconciliation to prevent incorrectly removing existing files.
            if inventory.truncated:
                logger.warning(
                    "Repository scan was truncated (%d files reached limit); "
                    "preserving existing index records and skipping deletion reconciliation.",
                    len(inventory.files),
                )
            else:
                for rel_path, (f_id, prev_hash, prev_mtime) in existing_files.items():
                    if rel_path not in scanned_map:
                        removed_paths.append(rel_path)

            for scanned_file in inventory.files:
                rel_path = scanned_file.relative_path
                if rel_path in existing_files:
                    _, prev_hash, prev_mtime = existing_files[rel_path]
                    # If content hash changed or file was touched with different hash
                    if scanned_file.content_sha256 != prev_hash:
                        to_index.append(scanned_file)
                else:
                    # New or renamed file
                    to_index.append(scanned_file)

            # 3. Handle deletions (only on complete scans)
            if removed_paths and not inventory.truncated:
                self.store.remove_files(removed_paths)
                files_removed = len(removed_paths)

            # 4. Parse and index modified/added files
            for scanned_file in to_index:
                rel_path = scanned_file.relative_path
                extracted: ExtractedFileSymbols | None = None
                parser = get_parser_for_language(scanned_file.language)

                # Only parse files within safe parse size bounds
                if parser is not None and scanned_file.size_bytes <= self.scanner.max_file_bytes:
                    try:
                        content = self.workspace.read_text(rel_path)
                        extracted = parser.parse(rel_path, content)
                    except Exception as exc:
                        logger.debug("Could not parse %s with %s: %s", rel_path, parser.__class__.__name__, exc)

                self.store.update_file_index(scanned_file, extracted)
                files_indexed += 1

            # 5. Extract and record dependencies
            deps_to_record: list[DependencyRecord] = []
            for manifest in inventory.manifests:
                for d in manifest.dependencies:
                    deps_to_record.append(
                        DependencyRecord(
                            ecosystem=manifest.ecosystem,
                            name=d,
                            scope="runtime",
                            source_file=manifest.manifest_path,
                        )
                    )
                for d in manifest.dev_dependencies:
                    deps_to_record.append(
                        DependencyRecord(
                            ecosystem=manifest.ecosystem,
                            name=d,
                            scope="dev",
                            source_file=manifest.manifest_path,
                        )
                    )

            # Replace dependency snapshot on every successful complete scan, including when empty
            if not inventory.truncated:
                self.store.record_dependencies(deps_to_record)
            elif deps_to_record:
                self.store.record_dependencies(deps_to_record)

            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            self.store.finish_run(
                run_id,
                status="completed",
                files_indexed=files_indexed,
                files_removed=files_removed,
                duration_ms=elapsed_ms,
            )

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            self.store.finish_run(
                run_id,
                status="failed",
                files_indexed=files_indexed,
                files_removed=files_removed,
                duration_ms=elapsed_ms,
                error_message=str(exc),
            )
            raise

        return self.store.get_stats()
