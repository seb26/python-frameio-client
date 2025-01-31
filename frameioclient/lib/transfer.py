from .bandwidth import DiskBandwidth, NetworkBandwidth
from .exceptions import (
    AssetChecksumMismatch,
    AssetChecksumNotPresent,
    AssetNotFullyUploaded,
    DownloadException,
    WatermarkIDDownloadException,
)
from .logger import SDKLogger
from .transport import HTTPClient
from .utils import FormatTypes, Utils

import concurrent.futures
import math
import os
import time
from datetime import datetime, timedelta
from random import randint
from typing import Any, Callable, Dict, List, Optional

import requests
from send2trash import send2trash

logger = SDKLogger.getLogger(__name__)

class FrameioDownloader(object):
    def __init__(
        self,
        asset: Dict,
        download_folder: str,
        prefix: str = None,
        multi_part: bool = False,
        replace: bool = False,
        use_temp_filename: bool = False,
    ):
        self.multi_part = multi_part
        self.asset = asset
        self.asset_type = None
        self.download_folder = download_folder
        self.replace = replace
        self.resolution_map = dict()
        self.destination = None
        self.destination_temp = None
        self.use_temp_filename = use_temp_filename
        self.watermarked = asset["is_session_watermarked"]  # Default is probably false
        self.filesize = asset["filesize"]
        self.futures = list()
        self.checksum = None
        self.original_checksum = None
        self.checksum_verification = True
        self.checksum_strict = False
        self.chunk_size = 25 * 1024 * 1024  # 25 MB chunk size
        self.chunks = math.ceil(self.filesize / self.chunk_size)
        self.prefix = prefix
        self.bytes_started = 0
        self.bytes_completed = 0
        self.in_progress = 0
        self.aws_client = None
        self.session = None
        self.filename = Utils.normalize_filename(asset["name"])
        self.request_logs = list()
        self.stats = True

        self._evaluate_asset()
        self._get_path()


    def _evaluate_asset(self):
        if self.asset.get("_type") != "file":
            raise DownloadException(
                message=f"Unsupport Asset type: {self.asset.get('_type')}"
            )

        # This logic may block uploads that were started before this field was introduced
        if self.asset.get("upload_completed_at") == None:
            raise AssetNotFullyUploaded

        try:
            self.original_checksum = self.asset["checksums"]["xx_hash"]
        except (TypeError, KeyError):
            self.original_checksum = None

    def _get_path(self):
        if self.prefix:
            self.filename = self.prefix + self.filename
        if self.destination is None:
            self.destination = os.path.join(self.download_folder, self.filename)
        if self.use_temp_filename:
            # Example: filename.mp4.tmp-8fa26e04
            self.destination_temp = os.path.join(self.destination, '.tmp-' + self.asset['id'][:8])
        return self.destination

    def _get_checksum(self):
        try:
            self.original_checksum = self.asset["checksums"]["xx_hash"]
        except (TypeError, KeyError):
            self.original_checksum = None
        return self.original_checksum
    
    def _checksum_verify(self, filepath: str):
        file_was_checksum_verified = False
        if self.downloader.checksum_verification is True:
            # Check for checksum, if not present throw error
            asset_sum = self.downloader._get_checksum()
            if asset_sum is None:
                logger.error(f"Checksum could not be verified, no xxhash checksum was listed.")
                if self.checksum_strict:
                    raise AssetChecksumNotPresent
                else:
                    return False
            # Calculate the file hash
            logger.debug('Calculating checksum...')
            disk_sum = Utils.calculate_hash(filepath)
            logger.debug(f"Asset: {asset_sum}; Disk {disk_sum}")
            if ( asset_sum == disk_sum ):
                file_was_checksum_verified = True
            else:
                if self.checksum_strict:
                    raise AssetChecksumMismatch
                else:
                    return False
        else:
            return False

    def _rename_from_temp_file(self):
        try:
            os.rename(
                self.destination_temp,
                self.destination
            )
        except Exception as e:
            logger.error(f"Unable to rename the temp file contents to the correct filename, see exception: {e}")
            logger.debug(e, exc_info=1)

    def get_download_key(self):
        try:
            url = self.asset["original"]
        except KeyError as e:
            if self.watermarked == True:
                resolution_list = list()
                try:
                    for resolution_key, download_url in sorted(
                        self.asset["downloads"].items()
                    ):
                        resolution = resolution_key.split("_")[
                            1
                        ]  # Grab the item at index 1 (resolution)
                        try:
                            resolution = int(resolution)
                        except ValueError:
                            continue

                        if download_url is not None:
                            resolution_list.append(download_url)

                    # Grab the highest resolution (first item) now
                    url = resolution_list[0]
                except KeyError:
                    raise DownloadException
            else:
                raise WatermarkIDDownloadException

        return url

    def download(
            self,
            stats: bool = False,
            progress_callback: Callable[[str], Any] = None,
        ):
        """Call this to perform the actual download of your asset!

        - stats: True to return a dict with stats about the download.
                 When `multi_part`=True, such stats always returned."""

        # Check folders
        if not os.path.isdir(os.path.join(os.path.curdir, self.download_folder)):
            os.mkdir(self.download_folder)

        # Check files
        filepath = self._get_path()
        if os.path.isfile(filepath):
            filesize_on_disk = os.path.getsize(filepath)
            if self.checksum_verification:
                result = self._checksum_verify(filepath)
                if result is True:
                    logger.info("File already exists at this location, and checksum matches. Skipping download.")
                else:
                    if self.replace is True:
                        logger.warning("File already exists at this location, and checksum does not match. `replace=True`, so it will be deleted on disk and redownloaded.")
                        logger.warning(f"Filesize on Frame.io: {self.filesize}; On disk: {filesize_on_disk}")
                        send2trash(filepath)
                        return {
                            "outcome_code": 0,
                            "outcome_file_exists": True,
                            "outcome_filesize_matched": None,
                            "outcome_checksum_matched": False,
                        }
                    else:
                        logger.warning("File already exists at this location, and checksum does not match. `replace=False`, so it will be skipped without action.")
                        return {
                            "outcome_code": 1,
                            "outcome_file_exists": True,
                            "outcome_filesize_matched": None,
                            "outcome_checksum_matched": False,
                        }
            else:
                if self.filesize == filesize_on_disk:
                    logger.info("File already exists at this location, matches filesize, no checksum available. Skipping download.")
                    return {
                        "outcome_code": 1,
                        "outcome_file_exists": True,
                        "outcome_filesize_matched": True,
                        "outcome_checksum_matched": False,
                    }
                else:
                    if self.replace is True:
                        logger.warning("File already exists at this location, and filesize does not match. `replace=True`, so it will be deleted on disk and redownloaded.")
                        return {
                            "outcome_code": 0,
                            "outcome_file_exists": True,
                            "outcome_filesize_matched": False,
                            "outcome_checksum_matched": False,
                        }
                    else:
                        logger.warning("File already exists at this location, and filesize does not match. `replace=False`, so it will be skipped without action.")
                        return {
                            "outcome_code": 1,
                            "outcome_file_exists": True,
                            "outcome_filesize_matched": False,
                            "outcome_checksum_matched": False,
                        }

        # Get URL
        url = self.get_download_key()

        # AWS Client
        self.aws_client = AWSClient(
            concurrency = 5,
            downloader = self,
            progress_callback = progress_callback,
        )

        # Handle watermarking
        if self.watermarked == True:
            return self.aws_client._download_whole(url)

        else:
            # Use multi-part download only for files greater than 25 MB
            if self.asset["filesize"] < 26214400:
                return self.aws_client._download_whole(url, stats)
            if self.multi_part == True:
                return self.aws_client._multi_thread_download()
            else:
                return self.aws_client._download_whole(url, stats)


class AWSClient(HTTPClient, object):
    def __init__(
        self,
        downloader: FrameioDownloader,
        concurrency = None,
        progress_callback: Callable[[str], Any] = None,
    ):
        super().__init__(self)  # Initialize via inheritance
        self.progress_callback = progress_callback
        self.progress_interval_sec = 5
        self.progress_manager = None
        self.destination = downloader.destination
        self.bytes_started = 0
        self.bytes_completed = 0
        self.downloader = downloader
        self.futures = []
        self.original = self.downloader.asset["original"]

        # Ensure this is a valid number before assigning
        if concurrency is not None and type(concurrency) == int and concurrency > 0:
            self.concurrency = concurrency
        # else:
        #     self.concurrency = self._optimize_concurrency()

    @staticmethod
    def check_cdn(url):
        # TODO improve this algo
        if "assets.frame.io" in url:
            return "Cloudfront"
        elif "s3" in url:
            return "S3"
        else:
            return None

    def _create_file_stub(self):
        try:
            fp = open(self.downloader.destination, "w")
            # fp.write(b"\0" * self.filesize) # Disabled to prevent pre-allocatation of disk space
            fp.close()
        except FileExistsError as e:
            if self.downloader.replace == True:
                logger.info(f"Creating file stub at below path and Downloader has replace=True. Deleting the file and creating new stub.\n  {self.downloader.destination}")
                os.remove(self.downloader.destination)  # Remove the file
                self._create_file_stub()  # Create a new stub
            else:
                logger.error(e)
                raise e
        except TypeError as e:
            logger.error(e)
            raise e
        return True

    def _optimize_concurrency(self):
        """
        This method looks as the net_stats and disk_stats that we've run on \
            the current environment in order to suggest the best optimized \
            number of concurrent TCP connections.

        Example::
            AWSClient._optimize_concurrency()
        """

        net_stats = NetworkBandwidth
        disk_stats = DiskBandwidth

        # Algorithm ensues
        #
        #

        return 5

    def _get_byte_range(
        self, url: str, start_byte: Optional[int] = 0, end_byte: Optional[int] = 2048
    ):
        """
        Get a specific byte range from a given URL. This is **not** optimized \
            for heavily-threaded operations currently.

        :Args:
            url (string): The URL you want to fetch a byte-range from
            start_byte (int): The first byte you want to request
            end_byte (int): The last byte you want to extract

        Example::
            AWSClient().get_byte_range(asset, "~./Downloads")
        """

        range_header = {"Range": "bytes=%d-%d" % (start_byte, end_byte)}

        headers = {**self.shared_headers, **range_header}

        br = requests.get(url, headers=headers).content
        return br

    def _download_whole(self, url: str, stats: bool=False):
        logger.info(
            "Beginning download -- {} -- {}".format(
                self.downloader.filename,
                Utils.format_value(self.downloader.filesize, type=FormatTypes.SIZE),
            )
        )
        start_time = time.time()
        self.session = self._get_session()
        r = self.session.get(url, stream=True)
        chunk_size = 4096
        chunk_count = 0
        bytes_downloaded = 0
        if self.downloader.use_temp_filename:
            filepath = self.downloader.destination_temp
        else:
            filepath = self.downloader.destination
        with open(filepath, "wb") as handle:
            if self.progress_callback:
                time_updated_last = datetime.now()
                time_update = time_updated_last + timedelta(seconds=self.progress_interval_sec)
                progress_values = {
                    'download_type': 'whole',
                    'start_time': start_time,
                    'end_time': None,
                    'status': 'incomplete',
                    'percent': 0,
                    'chunk_size': chunk_size,
                }
                self.progress_callback(**progress_values)
            try:
                # TODO make sure this approach works for SBWM download
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if chunk:
                        handle.write(chunk)
                        chunk_count += 1
                        bytes_downloaded += chunk_size
                    if self.progress_callback:
                        if datetime.now() >= time_update:
                            time_update = time_updated_last + timedelta(seconds=self.progress_interval_sec)
                            progress_values['status'] = 'incomplete'
                            progress_values['bytes_downloaded'] = bytes_downloaded
                            progress_values['percent'] = round( ( bytes_downloaded / self.downloader.filesize ) * 100, 2 )
                            self.progress_callback(**progress_values)
            except requests.exceptions.ChunkedEncodingError as e:
                logger.error(e, exc_info=1)
                if self.progress_callback:
                    progress_values['status'] = 'failed'
                    self.progress_callback(**progress_values)
                raise e
        end_time = time.time()
        if self.downloader.use_temp_filename:
            self.downloader._rename_from_temp_file()
        download_time = end_time - start_time
        if self.progress_callback:
            progress_values['status'] = 'complete'
            progress_values['end_time'] = end_time
            self.progress_callback(**progress_values)
        download_speed = Utils.format_value(
            math.ceil(self.downloader.filesize / (download_time))
        )
        logger.info(
            f"Downloaded {Utils.format_value(self.downloader.filesize, type=FormatTypes.SIZE)} at {download_speed}"
        )
        # Checksum verification
        if self.downloader.checksum_verification is True:
            file_was_checksum_verified = self._checksum_verify(self.destination)
        else:
            file_was_checksum_verified = False
        if stats:
            return {
                "outcome_code": 0,
                "outcome_description": "completed",
                "verification": file_was_checksum_verified,
                "destination": self.destination,
                "speed": download_speed,
                "elapsed": download_time,
                "cdn": AWSClient.check_cdn(self.original),
                "concurrency": self.concurrency,
                "size": self.downloader.filesize,
                "chunks": chunk_count,
                "chunk_size": chunk_size,
            }
        else:
            return self.destination, download_speed

    def _download_chunk(self, task: List):
        # Download a particular chunk
        # Called by the threadpool executor

        # Destructure the task object into its parts
        url = task[0]
        start_byte = task[1]
        end_byte = task[2]
        chunk_number = task[3]
        # in_progress = task[4]

        # Set the initial chunk_size, but prepare to overwrite
        chunk_size = end_byte - start_byte

        if self.bytes_started + (chunk_size) > self.downloader.filesize:
            difference = abs(
                self.downloader.filesize - (self.bytes_started + chunk_size)
            )  # should be negative
            chunk_size = chunk_size - difference
            logger.debug(f"Final chunk will be: {chunk_size}")
        else:
            pass

        # Set chunk size in a smarter way
        self.bytes_started += chunk_size

        # Specify the start and end of the range request
        headers = {"Range": "bytes=%d-%d" % (start_byte, end_byte)}

        # Grab the data as a stream
        self.session = self._get_session()
        r = self.session.get(url, headers=headers, stream=True)

        # Write the file to disk
        with open(self.destination, "r+b") as fp:
            fp.seek(start_byte)  # Seek to the right spot in the file
            chunk_size = len(r.content)  # Get the final chunk size
            fp.write(r.content)  # Write the data

        # Increase the count for bytes_completed, but only if it doesn't overrun file length
        self.bytes_completed += chunk_size
        if self.bytes_completed > self.downloader.filesize:
            self.bytes_completed = self.downloader.filesize

        # After the function completes, we report back the # of bytes transferred
        return chunk_size

    def _multi_thread_download(self):
        # Generate stub
        try:
            self._create_file_stub()
        except Exception as e:
            logger.error(f"Unable to create file stub")
            raise DownloadException(message=e)

        offset = math.ceil(self.downloader.filesize / self.downloader.chunks)
        in_byte = 0  # Set initially here, but then override

        logger.info(
            f"Begin multi-part download -- {self.downloader.asset['name']} -- {Utils.format_value(self.downloader.filesize, type=FormatTypes.SIZE)}"
        )

        start_time = time.time()
        if self.progress_callback:
            time_updated_last = datetime.now()
            time_update = time_updated_last + timedelta(seconds=self.progress_interval_sec)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.concurrency
        ) as executor:
            for i in range(int(self.downloader.chunks)):
                # Increment by the iterable + 1 so we don't mutiply by zero
                out_byte = offset * (i + 1)

                # Create task tuple
                task = (self.downloader.asset["original"], in_byte, out_byte, i)

                # Stagger start for each chunk by 0.1 seconds
                if i < self.concurrency:
                    time.sleep(randint(1, 5) / 10)

                # Append tasks to futures list
                self.futures.append(executor.submit(self._download_chunk, task))

                # Reset new in byte equal to last out byte
                in_byte = out_byte

            bytes_downloaded = 0
            if self.progress_callback:
                # Establish some callback values
                progress_values = {
                    'download_type': 'multi_thread',
                    'start_time': start_time,
                    'end_time': None,
                    'status': 'incomplete',
                    'chunks_num': self.downloader.chunks,
                    'percent': 0,
                }
            # Wait on threads to finish
            for future in concurrent.futures.as_completed(self.futures):
                try:
                    chunk_size = future.result()
                    bytes_downloaded += chunk_size
                    bytes_downloaded_percent = round( ( bytes_downloaded / self.downloader.filesize ) * 100, 2 )
                    logger.debug(f"This chunk size: {chunk_size}")
                    if self.progress_callback:
                        if datetime.now() >= time_update:
                            time_update = time_updated_last + timedelta(seconds=self.progress_interval_sec)
                            progress_values['status'] = 'incomplete'
                            progress_values['bytes_downloaded'] = bytes_downloaded
                            progress_values['chunk_size'] = chunk_size
                            progress_values['percent'] = bytes_downloaded_percent
                            self.progress_callback(**progress_values)
                except Exception as e:
                    progress_values['status'] = 'failed'
                    logger.error(e, exc_info=1)
                    if self.progress_callback:
                        self.progress_callback(**progress_values)
        end_time = time.time()
        if self.downloader.use_temp_filename:
            self.downloader._rename_from_temp_file()
        # Callback
        if self.progress_callback:
            progress_values['status'] = 'complete'
            progress_values['end_time'] = end_time
            self.progress_callback(**progress_values)
        # Calculate and print stats
        download_time = round((end_time - start_time), 2)
        download_speed = round((self.downloader.filesize / download_time), 2)
        # Log completion event
        logger.info(
            f"Downloaded {Utils.format_value(self.downloader.filesize, type=FormatTypes.SIZE)} at {Utils.format_value(download_speed, type=FormatTypes.SPEED)}"
        )
        # Checksum verification
        if self.downloader.checksum_verification is True:
            file_was_checksum_verified = self._checksum_verify(self.destination)
        else:
            file_was_checksum_verified = False
        if self.downloader.stats:
            return {
                "status_code": 0,
                "status_description": "completed",
                "verified": file_was_checksum_verified,
                "destination": self.destination,
                "speed": download_speed,
                "elapsed": download_time,
                "cdn": AWSClient.check_cdn(self.original),
                "concurrency": self.concurrency,
                "size": self.downloader.filesize,
                "chunks_num": self.downloader.chunks,
            }
        else:
            return self.destination


class TransferJob(AWSClient):
    # These will be used to track the job and then push telemetry
    def __init__(self, job_info):
        self.job_info = job_info  # < - convert to JobInfo class
        self.cdn = "S3"  # or 'CF' - use check_cdn to confirm
        self.progress_manager = None


class DownloadJob(TransferJob):
    def __init__(self):
        self.asset_type = "review_link"  # we should use a dataclass here
        # Need to create a re-usable job schema
        # Think URL -> output_path
        pass


class UploadJob(TransferJob):
    def __init__(self, destination):
        self.destination = destination
        # Need to create a re-usable job schema
        # Think local_file path and remote Frame.io destination
        pass
