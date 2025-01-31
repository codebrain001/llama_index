"""Google Drive files reader."""

import logging
import os
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials

from llama_index.core.readers import SimpleDirectoryReader
from llama_index.core.readers.base import BaseReader, BasePydanticReader
from llama_index.core.bridge.pydantic import PrivateAttr, Field
from llama_index.core.schema import Document

logger = logging.getLogger(__name__)

# Scope for reading and downloading google drive files
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


class GoogleDriveReader(BasePydanticReader):
    """Google Drive Reader.

    Reads files from Google Drive. Credentials passed directly to the constructor
    will take precedence over those passed as file paths.

    Args:
        is_cloud (Optional[bool]): Whether the reader is being used in
            a cloud environment. Will not save credentials to disk if so.
            Defaults to False.
        credentials_path (Optional[str]): Path to client config file.
            Defaults to None.
        token_path (Optional[str]): Path to authorized user info file. Defaults
            to None.
        service_account_key_path (Optional[str]): Path to service account key
            file. Defaults to None.
        client_config (Optional[dict]): Dictionary containing client config.
            Defaults to None.
        authorized_user_info (Optional[dict]): Dicstionary containing authorized
            user info. Defaults to None.
        service_account_key (Optional[dict]): Dictionary containing service
            account key. Defaults to None.
        file_extractor (Optional[Dict[str, BaseReader]]): A mapping of file
            extension to a BaseReader class that specifies how to convert that
            file to text. See `SimpleDirectoryReader` for more details.
    """

    client_config: Optional[dict] = None
    authorized_user_info: Optional[dict] = None
    service_account_key: Optional[dict] = None
    token_path: Optional[str] = None
    file_extractor: Optional[Dict[str, Union[str, BaseReader]]] = Field(
        default=None, exclude=True
    )

    _is_cloud: bool = PrivateAttr(default=False)
    _creds: Credentials = PrivateAttr()
    _mimetypes: dict = PrivateAttr()

    def __init__(
        self,
        is_cloud: Optional[bool] = False,
        credentials_path: str = "credentials.json",
        token_path: str = "token.json",
        service_account_key_path: str = "service_account_key.json",
        client_config: Optional[dict] = None,
        authorized_user_info: Optional[dict] = None,
        service_account_key: Optional[dict] = None,
        file_extractor: Optional[Dict[str, Union[str, BaseReader]]] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize with parameters."""
        self._creds = None
        self._is_cloud = (is_cloud,)
        # Download Google Docs/Slides/Sheets as actual files
        # See https://developers.google.com/drive/v3/web/mime-types
        self._mimetypes = {
            "application/vnd.google-apps.document": {
                "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "extension": ".docx",
            },
            "application/vnd.google-apps.spreadsheet": {
                "mimetype": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ),
                "extension": ".xlsx",
            },
            "application/vnd.google-apps.presentation": {
                "mimetype": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "extension": ".pptx",
            },
        }

        # Read the file contents so they can be serialized and stored.
        if client_config is None and os.path.isfile(credentials_path):
            with open(credentials_path, encoding="utf-8") as json_file:
                client_config = json.load(json_file)

        if authorized_user_info is None and os.path.isfile(token_path):
            with open(token_path, encoding="utf-8") as json_file:
                authorized_user_info = json.load(json_file)

        if service_account_key is None and os.path.isfile(service_account_key_path):
            with open(service_account_key_path, encoding="utf-8") as json_file:
                service_account_key = json.load(json_file)

        if client_config is None and service_account_key is None:
            raise ValueError("Must specify `client_config` or `service_account_key`.")

        super().__init__(
            client_config=client_config,
            authorized_user_info=authorized_user_info,
            service_account_key=service_account_key,
            token_path=token_path,
            file_extractor=file_extractor,
            **kwargs,
        )

    @classmethod
    def class_name(cls) -> str:
        return "GoogleDriveReader"

    def _get_credentials(self) -> Tuple[Credentials]:
        """Authenticate with Google and save credentials.
        Download the service_account_key.json file with these instructions: https://cloud.google.com/iam/docs/keys-create-delete.

        Returns:
            credentials
        """
        from google_auth_oauthlib.flow import InstalledAppFlow

        # First, we need the Google API credentials for the app
        creds = None

        if self.authorized_user_info is not None:
            creds = Credentials.from_authorized_user_info(
                self.authorized_user_info, SCOPES
            )
        elif self.service_account_key is not None:
            return service_account.Credentials.from_service_account_info(
                self.service_account_key, scopes=SCOPES
            )

        # If there are no (valid) credentials available, let the user log in.
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_config(self.client_config, SCOPES)
                creds = flow.run_local_server(port=0)

            # Save the credentials for the next run
            if not self._is_cloud:
                with open(self.token_path, "w", encoding="utf-8") as token:
                    token.write(creds.to_json())

        return creds

    def _get_fileids_meta(
        self,
        drive_id: Optional[str] = None,
        folder_id: Optional[str] = None,
        file_id: Optional[str] = None,
        mime_types: Optional[List[str]] = None,
        query_string: Optional[str] = None,
    ) -> List[List[str]]:
        """Get file ids present in folder/ file id
        Args:
            drive_id: Drive id of the shared drive in google drive.
            folder_id: folder id of the folder in google drive.
            file_id: file id of the file in google drive
            mime_types: The mimeTypes you want to allow e.g.: "application/vnd.google-apps.document"
            query_string: A more generic query string to filter the documents, e.g. "name contains 'test'".

        Returns:
            metadata: List of metadata of filde ids.
        """
        from googleapiclient.discovery import build

        try:
            service = build("drive", "v3", credentials=self._creds)
            fileids_meta = []
            if folder_id:
                folder_mime_type = "application/vnd.google-apps.folder"
                query = "('" + folder_id + "' in parents)"

                # Add mimeType filter to query
                if mime_types:
                    if folder_mime_type not in mime_types:
                        mime_types.append(folder_mime_type)  # keep the recursiveness
                    mime_query = " or ".join(
                        [f"mimeType='{mime_type}'" for mime_type in mime_types]
                    )
                    query += f" and ({mime_query})"

                # Add query string filter
                if query_string:
                    # to keep the recursiveness, we need to add folder_mime_type to the mime_types
                    query += (
                        f" and ((mimeType='{folder_mime_type}') or ({query_string}))"
                    )

                items = []
                # get files taking into account that the results are paginated
                while True:
                    if drive_id:
                        results = (
                            service.files()
                            .list(
                                q=query,
                                driveId=drive_id,
                                corpora="drive",
                                includeItemsFromAllDrives=True,
                                supportsAllDrives=True,
                                fields="*",
                            )
                            .execute()
                        )
                    else:
                        results = (
                            service.files()
                            .list(
                                q=query,
                                includeItemsFromAllDrives=True,
                                supportsAllDrives=True,
                                fields="*",
                            )
                            .execute()
                        )
                    items.extend(results.get("files", []))
                    page_token = results.get("nextPageToken", None)
                    if page_token is None:
                        break

                for item in items:
                    if item["mimeType"] == folder_mime_type:
                        if drive_id:
                            fileids_meta.extend(
                                self._get_fileids_meta(
                                    drive_id=drive_id,
                                    folder_id=item["id"],
                                    mime_types=mime_types,
                                    query_string=query_string,
                                )
                            )
                        else:
                            fileids_meta.extend(
                                self._get_fileids_meta(
                                    folder_id=item["id"],
                                    mime_types=mime_types,
                                    query_string=query_string,
                                )
                            )
                    else:
                        # Check if file doesn't belong to a Shared Drive. "owners" doesn't exist in a Shared Drive
                        is_shared_drive = "driveId" in item
                        author = (
                            item["owners"][0]["displayName"]
                            if not is_shared_drive
                            else "Shared Drive"
                        )

                        fileids_meta.append(
                            (
                                item["id"],
                                author,
                                item["name"],
                                item["mimeType"],
                                item["createdTime"],
                                item["modifiedTime"],
                            )
                        )
            else:
                # Get the file details
                file = (
                    service.files()
                    .get(fileId=file_id, supportsAllDrives=True, fields="*")
                    .execute()
                )
                # Get metadata of the file
                # Check if file doesn't belong to a Shared Drive. "owners" doesn't exist in a Shared Drive
                is_shared_drive = "driveId" in file
                author = (
                    file["owners"][0]["displayName"]
                    if not is_shared_drive
                    else "Shared Drive"
                )

                fileids_meta.append(
                    (
                        file["id"],
                        author,
                        file["name"],
                        file["mimeType"],
                        file["createdTime"],
                        file["modifiedTime"],
                    )
                )
            return fileids_meta

        except Exception as e:
            logger.error(
                f"An error occurred while getting fileids metadata: {e}", exc_info=True
            )

    def _download_file(self, fileid: str, filename: str) -> str:
        """Download the file with fileid and filename
        Args:
            fileid: file id of the file in google drive
            filename: filename with which it will be downloaded
        Returns:
            The downloaded filename, which which may have a new extension.
        """
        from io import BytesIO

        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload

        try:
            # Get file details
            service = build("drive", "v3", credentials=self._creds)
            file = service.files().get(fileId=fileid, supportsAllDrives=True).execute()

            if file["mimeType"] in self._mimetypes:
                download_mimetype = self._mimetypes[file["mimeType"]]["mimetype"]
                download_extension = self._mimetypes[file["mimeType"]]["extension"]
                new_file_name = filename + download_extension

                # Download and convert file
                request = service.files().export_media(
                    fileId=fileid, mimeType=download_mimetype
                )
            else:
                new_file_name = filename

                # Download file without conversion
                request = service.files().get_media(fileId=fileid)

            # Download file data
            file_data = BytesIO()
            downloader = MediaIoBaseDownload(file_data, request)
            done = False

            while not done:
                status, done = downloader.next_chunk()

            # Save the downloaded file
            with open(new_file_name, "wb") as f:
                f.write(file_data.getvalue())

            return new_file_name
        except Exception as e:
            logger.error(
                f"An error occurred while downloading file: {e}", exc_info=True
            )

    def _load_data_fileids_meta(self, fileids_meta: List[List[str]]) -> List[Document]:
        """Load data from fileids metadata
        Args:
            fileids_meta: metadata of fileids in google drive.

        Returns:
            Lis[Document]: List of Document of data present in fileids.
        """
        try:
            with tempfile.TemporaryDirectory() as temp_dir:

                def get_metadata(filename):
                    return metadata[filename]

                temp_dir = Path(temp_dir)
                metadata = {}

                for fileid_meta in fileids_meta:
                    # Download files and name them with their fileid
                    fileid = fileid_meta[0]
                    filepath = os.path.join(temp_dir, fileid)
                    final_filepath = self._download_file(fileid, filepath)

                    # Add metadata of the file to metadata dictionary
                    metadata[final_filepath] = {
                        "file id": fileid_meta[0],
                        "author": fileid_meta[1],
                        "file name": fileid_meta[2],
                        "mime type": fileid_meta[3],
                        "created at": fileid_meta[4],
                        "modified at": fileid_meta[5],
                    }
                loader = SimpleDirectoryReader(
                    temp_dir,
                    file_extractor=self.file_extractor,
                    file_metadata=get_metadata,
                )
                documents = loader.load_data()
                for doc in documents:
                    doc.id_ = doc.metadata.get("file id", doc.id_)

            return documents
        except Exception as e:
            logger.error(
                f"An error occurred while loading data from fileids meta: {e}",
                exc_info=True,
            )

    def _load_from_file_ids(
        self,
        drive_id: Optional[str],
        file_ids: List[str],
        mime_types: Optional[List[str]],
        query_string: Optional[str],
    ) -> List[Document]:
        """Load data from file ids
        Args:
            file_ids: File ids of the files in google drive.
            mime_types: The mimeTypes you want to allow e.g.: "application/vnd.google-apps.document"
            query_string: List of query strings to filter the documents, e.g. "name contains 'test'".

        Returns:
            Document: List of Documents of text.
        """
        try:
            fileids_meta = []
            for file_id in file_ids:
                fileids_meta.extend(
                    self._get_fileids_meta(
                        drive_id=drive_id,
                        file_id=file_id,
                        mime_types=mime_types,
                        query_string=query_string,
                    )
                )
            return self._load_data_fileids_meta(fileids_meta)
        except Exception as e:
            logger.error(
                f"An error occurred while loading with fileid: {e}", exc_info=True
            )

    def _load_from_folder(
        self,
        drive_id: Optional[str],
        folder_id: str,
        mime_types: Optional[List[str]],
        query_string: Optional[str],
    ) -> List[Document]:
        """Load data from folder_id.

        Args:
            drive_id: Drive id of the shared drive in google drive.
            folder_id: folder id of the folder in google drive.
            mime_types: The mimeTypes you want to allow e.g.: "application/vnd.google-apps.document"
            query_string: A more generic query string to filter the documents, e.g. "name contains 'test'".

        Returns:
            Document: List of Documents of text.
        """
        try:
            fileids_meta = self._get_fileids_meta(
                drive_id=drive_id,
                folder_id=folder_id,
                mime_types=mime_types,
                query_string=query_string,
            )
            return self._load_data_fileids_meta(fileids_meta)
        except Exception as e:
            logger.error(
                f"An error occurred while loading from folder: {e}", exc_info=True
            )

    def load_data(
        self,
        drive_id: Optional[str] = None,
        folder_id: Optional[str] = None,
        file_ids: Optional[List[str]] = None,
        mime_types: Optional[List[str]] = None,  # Deprecated
        query_string: Optional[str] = None,
    ) -> List[Document]:
        """Load data from the folder id or file ids.

        Args:
            drive_id: Drive id of the shared drive in google drive.
            folder_id: Folder id of the folder in google drive.
            file_ids: File ids of the files in google drive.
            mime_types: The mimeTypes you want to allow e.g.: "application/vnd.google-apps.document"
            query_string: A more generic query string to filter the documents, e.g. "name contains 'test'".
                It gives more flexibility to filter the documents. More info: https://developers.google.com/drive/api/v3/search-files

        Returns:
            List[Document]: A list of documents.
        """
        self._creds = self._get_credentials()

        if folder_id:
            return self._load_from_folder(drive_id, folder_id, mime_types, query_string)
        elif file_ids:
            return self._load_from_file_ids(
                drive_id, file_ids, mime_types, query_string
            )
        else:
            logger.warning("Either 'folder_id' or 'file_ids' must be provided.")
            return []
