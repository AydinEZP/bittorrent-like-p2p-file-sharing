from tests.test_metainfo import main as run_metainfo_tests
from tests.test_logger import main as run_logger_tests
from tests.test_tracker_state import main as run_tracker_state_tests
from tests.test_tracker_http import main as run_tracker_http_tests
from tests.test_tracker_client import main as run_tracker_client_tests
from tests.test_peer_ping import main as run_peer_ping_tests
from tests.test_piece_transfer import main as run_piece_transfer_tests
from tests.test_torrent_thread_pool import main as run_thread_pool_tests
from tests.test_final_integration import main as run_final_integration_tests


def main() -> None:
    test_groups = (
        run_metainfo_tests,
        run_logger_tests,
        run_tracker_state_tests,
        run_tracker_http_tests,
        run_tracker_client_tests,
        run_peer_ping_tests,
        run_piece_transfer_tests,
        run_thread_pool_tests,
        run_final_integration_tests,
    )

    for index, test_group in enumerate(test_groups):
        if index:
            print()
        test_group()

    print()
    print("ALL TESTS RESULT: PASS")


if __name__ == "__main__":
    main()
