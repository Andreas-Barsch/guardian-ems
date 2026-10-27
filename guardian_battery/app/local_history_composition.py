"""Explicit local-only wiring of real API/reader classes to prepared stores.

No discovery, preparation, worker start, or production selection occurs here.
Callers own catalogue registration, immutable metadata and publication indexes.
"""
from dataclasses import dataclass
from canonical_phase import CanonicalPhaseReader
from display_history_reader import DisplayHistoryReader
from event_overlay import EventOverlayAdapter
from history_api import HistoryApi
from history_series import CellHistorySeries
from hycube_evidence import HycubeBatteryCapacitySeries
from indexed_history_window import IndexedHistoryWindow
from repository_history_readers import PublishedHistoryReaders
from research_api import GuardianResearchApi


@dataclass(frozen=True)
class LocalHistoryComposition:
    history: HistoryApi
    research: GuardianResearchApi
    published: PublishedHistoryReaders


def compose_local_history(*, paths, cell_catalog, metadata, publication_index,
                          canonical_repository, display_repository, streams,
                          scheduler, cursor_secret, hycube_catalog=None,
                          rs485_catalog=None, current_config_provider=None, cell_registry=None,
                          hycube_registry=None, rs485_registry=None, policy_reader=None):
    from application_metadata import HeadMetadataReader
    read_scope = metadata.capture if isinstance(metadata, HeadMetadataReader) else None
    from confirmed_prefix_reader import ConfirmedPrefixReader
    def confirmed_window(catalog, registry):
        if registry is not None and catalog is None:
            raise ValueError('confirmed registry requires its explicit source catalog')
        return (IndexedHistoryWindow(catalog, prefix_reader=(
            ConfirmedPrefixReader(registry) if registry is not None else None))
            if catalog is not None else None)
    window = confirmed_window(cell_catalog, cell_registry)
    hycube_window = confirmed_window(hycube_catalog, hycube_registry)
    rs485_window = confirmed_window(rs485_catalog, rs485_registry)
    cells = CellHistorySeries(paths.cell_history, window_backend=window,
                              position_window=metadata.position_window)
    hycube = (HycubeBatteryCapacitySeries(paths.hycube_history,
        projection_directory=paths.hycube_projection,
        window_backend=hycube_window)
        if hycube_catalog is not None else None)
    from rs485_evidence import Rs485HistorySeries
    rs485_reader = (Rs485HistorySeries(paths.rs485_history,
        position_history_path=paths.position_history,
        window_backend=rs485_window,identity_backend=metadata)
        if rs485_catalog is not None else None)
    published = PublishedHistoryReaders(publication_index, canonical_repository,
        display_repository, streams=streams, full_series=cells, full_hycube=hycube)
    display = DisplayHistoryReader(paths.display_history, paths.cell_history,
        paths.hycube_projection or paths.cell_history, cells, hycube,
        window_backend=published)
    phases = CanonicalPhaseReader(paths.canonical_phase, window_backend=published)
    from indexed_phase_engine import IndexedPhaseEngine
    from history_timing import HistoryRequestTimingState
    history = HistoryApi(cells, EventOverlayAdapter(metadata),
        phase_engine=IndexedPhaseEngine(metadata, current_config_provider),
        timing_state=HistoryRequestTimingState(),
        rs485_series=rs485_reader, hycube_series=hycube, display_reader=display,
        canonical_phase_reader=phases, scheduler=scheduler, read_scope=read_scope)
    from prepared_daily_reader import SelectedDailyRepository
    research = GuardianResearchApi(paths, cursor_secret=cursor_secret,
        window_backend=window, identity_backend=metadata, hycube_reader=hycube,
        display_reader=display, canonical_reader=phases, scheduler=scheduler, read_scope=read_scope, rs485_window=rs485_window, daily_repository_factory=SelectedDailyRepository, policy_reader=policy_reader)
    return LocalHistoryComposition(history, research, published)
