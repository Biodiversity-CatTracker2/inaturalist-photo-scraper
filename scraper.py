#!/usr/bin/env python
# coding: utf-8

import argparse
import hashlib
import io
import os
import re
import signal
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from loguru import logger
from minio import Minio
from minio.error import InvalidResponseError
from requests.exceptions import HTTPError, JSONDecodeError
from requests.structures import CaseInsensitiveDict


class iNatPhotoScraper:

    def __init__(self,
                 taxon_id: int,
                 output_dir: Optional[str] = None,
                 resume_from_page: int = 0,
                 stop_at_page: Optional[int] = None,
                 resume_from_uuid_index: int = 0,
                 upload_to_s3: bool = True,
                 one_page_only: bool = True):
        super(iNatPhotoScraper, self).__init__()
        self.taxon_id = taxon_id
        self.output_dir = output_dir
        self.resume_from_page = resume_from_page
        self.stop_at_page = stop_at_page
        self.resume_from_uuid_index = resume_from_uuid_index
        self.upload_to_s3 = upload_to_s3
        self.one_page_only = one_page_only
        self.logger = self._logger()
        self.s3 = self._s3_client()
        self.data = {
            'uuids': [],
            'observations': [],
            'failed_observations': [],
            'failed_downloads': []
        }

    def _s3_client(self):
        s3_client = None
        if self.upload_to_s3:
            s3_endpoint = re.sub(r'https?:\/\/', '', os.environ['S3_ENDPOINT'])
            s3_client = Minio(s3_endpoint,
                              access_key=os.environ['S3_ACCESS_KEY'],
                              secret_key=os.environ['S3_SECRET_KEY'])
        return s3_client

    def _logger(self):
        logger.remove()
        Path('logs').mkdir(exist_ok=True)
        logger.add(
            sys.stderr,
            format='{level.icon} <fg #3bd6c6>{time:HH:mm:ss}</fg #3bd6c6> | '
            '<level>{level: <8}</level> | '
            '<lvl>{message}</lvl>',
            level='DEBUG')
        logger.level('WARNING', color='<yellow><bold>', icon='🚧')
        logger.level('INFO', color='<bold>', icon='🚀')
        logger.add(f'logs/{self.taxon_id}.log')
        return logger

    def _keyboard_interrupt_handler(self, sig: int, _) -> None:
        self.logger.info(f'>>>>>>>>>> Latest page: {self.resume_from_page}')
        self.logger.info(
            f'>>>>>>>>>> Latest UUID index: {self.resume_from_uuid_index}')
        self.logger.warning(
            f'Failed observations: {self.data["failed_observations"]}')
        self.logger.warning(
            f'Failed downloads: {self.data["failed_downloads"]}')
        self.logger.warning(
            f'\nKeyboardInterrupt (id: {sig}) has been caught...')
        self.logger.warning('Terminating the session gracefully...')
        sys.exit(1)

    @staticmethod
    def _encode_params(params: dict) -> str:
        return '&'.join(
            [f'{k}={urllib.parse.quote(str(v))}' for k, v in params.items()])

    def _get_request(self,
                     url: str,
                     params: Optional[dict] = None,
                     as_json: bool = True,
                     **kwargs):
        headers = CaseInsensitiveDict()
        headers['accept'] = 'application/json'

        if params:
            encoded_params = self._encode_params(params)
            url = f'{url}?{encoded_params}'

        try:
            r = requests.get(url, headers=headers, **kwargs)
            r.raise_for_status()
            if as_json:
                return r.json()
            else:
                return r
        except (HTTPError, JSONDecodeError) as e:
            self.logger.error(f'Failed to get {url}! (ERROR: {e})')
            self.logger.exception(e)
            self.data['failed_observations'].append(url)
            return
        finally:
            time.sleep(1)

    def _get_num_pages(self):
        url = 'https://api.inaturalist.org/v2/observations'
        params = {'taxon_id': self.taxon_id}
        r = self._get_request(url, params=params)
        if not r:
            sys.exit('Failed to ger number of pages!')
        total_results = r['total_results']
        return total_results // 200 + 1

    def get_observations_uuids(self, page: int) -> list:
        url = 'https://api.inaturalist.org/v2/observations'
        params = {
            'taxon_id': self.taxon_id,
            'photos': 'true',
            'page': page,
            'per_page': 200,
            'order': 'asc',
            'order_by': 'created_at',
            'fields': 'uuid'
        }

        resp_json = self._get_request(url, params=params)
        if not resp_json:
            return

        uuids = [x['uuid'] for x in resp_json['results']]
        return uuids

    def download_photos(self, _uuid):
        url = f'https://www.inaturalist.org/observations/{_uuid}.json'
        self.logger.debug(f'({_uuid}) Requesting observation')

        observation = self._get_request(url, allow_redirects=True)
        if not observation:
            self.data['failed_observations'].append(_uuid)
            return

        self.data['observations'].append(observation)
        observation_photos = observation['observation_photos']
        if not observation_photos:
            self.logger.debug(f'({_uuid}) No photos... Skipping...')
            return

        for photo in observation_photos:
            photo_url = photo['photo']['large_url']
            photo_uuid = photo['photo']['uuid']
            self.logger.debug(f'({photo_uuid}) Downloading...')

            suffix = Path(photo_url).suffix.lower()
            if not suffix or suffix == '.jpeg':
                suffix = '.jpg'

            r = self._get_request(photo_url, as_json=False)
            if not r:
                self.logger.error(
                    f'Could not download {photo_url}! (ERROR: {e})')
                self.data['failed_downloads'].append(photo_url)
                continue

            fname = hashlib.md5(r.content).hexdigest() + suffix

            if self.upload_to_s3:
                try:
                    self.s3.put_object(os.environ['S3_BUCKET_NAME'],
                                       fname,
                                       io.BytesIO(r.content),
                                       length=-1,
                                       part_size=10 * 1024 * 1024)
                except InvalidResponseError:
                    self.data['failed_downloads'].append(photo)
            else:
                with open(Path(f'{self.output_dir}/{fname}'), 'wb') as f:
                    f.write(r.content)
            self.logger.debug(f'({photo_uuid}) ✅ Downloaded')
        return True

    def run(self):
        signal.signal(signal.SIGINT, self._keyboard_interrupt_handler)

        if not self.output_dir:
            self.output_dir = f'downloaded_images_{self.taxon_id}'

        if not self.upload_to_s3:
            Path(self.output_dir).mkdir(exist_ok=True, parents=True)

        num_pages = self._get_num_pages()
        pages_range = range(num_pages)
        self.logger.info(f'Number of pages: {num_pages}')
        self.logger.info(
            f'Estimated number of observations: {num_pages * 200}')

        pages_range = pages_range[self.resume_from_page:]

        for page in pages_range:
            if page == self.stop_at_page:
                break
            self.logger.info(f'Current page: {page}')
            self.resume_from_page = page
            uuids = self.get_observations_uuids(page)

            if not uuids:
                self.data['failed_observations'].append(f'failed page: {page}')
                if self.one_page_only:
                    break
                else:
                    continue

            if uuids in self.data['uuids']:
                self.logger.warning(f'Duplicate response in page {page}! '
                                    'Skipping...')
                continue
            self.data['uuids'] += uuids
            uuids = uuids[self.resume_from_uuid_index:]

            for n, _uuid in enumerate(uuids,
                                      start=self.resume_from_uuid_index):
                self.resume_from_uuid_index = n
                self.logger.debug(f'Page: {page}, UUID index: {n}')
                res = self.download_photos(_uuid)

            if self.one_page_only:
                break


def _opts() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('-t',
                        '--taxon-id',
                        help='Taxon id',
                        type=int,
                        required=True)
    parser.add_argument('-o',
                        '--output-dir',
                        help='Output directory',
                        type=str)
    parser.add_argument('-s',
                        '--resume-from-page',
                        help='Page to resume from',
                        type=int,
                        default=0)
    parser.add_argument('-e',
                        '--stop-at-page',
                        help='Page to stop at',
                        type=int)
    parser.add_argument('-u',
                        '--resume-from-uuid-index',
                        help='UUID index to resume from',
                        type=int,
                        default=0)
    parser.add_argument('--upload-to-s3',
                        help='Upload to a S3-compatible bucket',
                        action='store_true')
    parser.add_argument('--one-page-only',
                        help='Terminate after completing a single page',
                        action='store_true')
    return parser.parse_args()


if __name__ == '__main__':
    load_dotenv()
    args = _opts()
    scraper = iNatPhotoScraper(
        taxon_id=args.taxon_id,
        output_dir=args.output_dir,
        resume_from_page=args.resume_from_page,
        stop_at_page=args.stop_at_page,
        resume_from_uuid_index=args.resume_from_uuid_index,
        upload_to_s3=args.upload_to_s3)
    scraper.run()
