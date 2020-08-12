import json
import re
from abc import ABC, abstractmethod
from collections.abc import MutableMapping, Hashable
from enum import Enum
from pathlib import Path, PureWindowsPath, PurePath, PurePosixPath
from typing import Iterator, Optional, Any, List, Dict

from paquo._base import QuPathBase
from paquo._logging import redirect, get_logger
from paquo._utils import cached_property
from paquo.hierarchy import QuPathPathObjectHierarchy
from paquo.java import String, DefaultProjectImageEntry, ImageType, ImageData, IOException, URI, URISyntaxException, \
    PathIO, File, BufferedImage

_log = get_logger(__name__)


class ImageProvider(ABC):
    """Maps image ids to paths and paths to image ids."""

    @abstractmethod
    def uri(self, image_id: Hashable) -> Optional[str]:
        """Returns an URI for an image given an image id tuple."""
        # default implementation:
        # -> null uri
        return None

    @abstractmethod
    def id(self, uri: str) -> Hashable:
        """Returns an image id given an URI."""
        # default implementation:
        # -> return filename as image id
        return ImageProvider.path_from_uri(uri).name

    @abstractmethod
    def rebase(self, *uris: str, **kwargs) -> List[Optional[str]]:
        """Allows rebasing"""
        return [self.uri(self.id(uri)) for uri in uris]

    @staticmethod
    def path_from_uri(uri: str) -> PurePath:
        """
        Parses an URI representing a file system path into a Path.
        """
        try:
            java_uri = URI(uri)
        except URISyntaxException:
            raise ValueError(f"not a valid uri '{uri}'")

        if str(java_uri.getScheme()) != "file":
            raise NotImplementedError("paquo only supports file:/ URIs as of now")
        path_str = str(java_uri.getPath())

        # check if we encode a windows path
        # fixme: not sure if there's a better way to do this...
        if re.match("/[A-Z]:/", path_str):
            return PureWindowsPath(path_str[1:])
        else:
            return PurePosixPath(path_str)

    @staticmethod
    def uri_from_path(path: PurePath) -> str:
        """
        Convert a python path object to an URI
        """
        if not path.is_absolute():
            raise ValueError("uri_from_path requires an absolute path")
        return str(URI(path.as_uri()).toString())

    @staticmethod
    def compare_uris(a: str, b: str) -> bool:
        # ... comma encoding is problematic
        # python url encodes commas, but java doesn't
        # need to add more tests
        uri_a = URI(a)
        uri_b = URI(b)
        if any(str(uri.getScheme()) != "file" for uri in [uri_a, uri_b]):
            raise NotImplementedError("currently untested ...")
        return bool(uri_a.getPath() == uri_b.getPath())

    @classmethod
    def __subclasshook__(cls, C):
        """ImageProviders don't need to derive but only duck-type"""
        required_methods = ('uri', 'id', 'rebase')
        if cls is ImageProvider:
            methods_available = [False] * len(required_methods)
            for B in C.__mro__:
                for idx, method in enumerate(required_methods):
                    methods_available[idx] |= method in B.__dict__
                if all(methods_available):
                    return True
        return NotImplemented


# noinspection PyMethodMayBeStatic
class SimpleURIImageProvider:
    """simple image provider that uses the files uri as it's identifier"""

    class URIString(str):
        """string uri's can differ in their string representation and still be identical"""
        # we need some way to normalize uris
        def __eq__(self, other):
            return ImageProvider.compare_uris(self, other)
        __hash__ = str.__hash__  # fixme: this is not correct!

    def uri(self, image_id: str) -> str:
        # fixme: this is currently not being called and i think it
        #   should in the default implementation... need to figure
        #   out where this needs to be used in QuPathProject
        return image_id

    def id(self, uri: str) -> str:
        return self.URIString(uri)

    def rebase(self, *uris: str, **kwargs) -> List[Optional[str]]:
        uri2uri = kwargs.pop('uri2uri', {})
        return [uri2uri.get(uri, None) for uri in uris]


# noinspection PyPep8Naming
class _RecoveredReadOnlyImageServer:
    """internal. used to allow access to image server metadata recovered from project.qpproj"""
    def __init__(self, entry_path: Path):
        server_json_f = Path(entry_path) / "server.json"
        with server_json_f.open('r') as f:
            self._metadata = json.load(f).get('metadata', {})

    def getWidth(self):
        return self._metadata['width']

    def getHeight(self):
        return self._metadata['height']

    def nChannels(self):
        return len(self._metadata['channels'])

    def nZSlices(self):
        return self._metadata['sizeZ']

    def nTimepoints(self):
        return self._metadata['sizeT']

    def getMetadata(self) -> Any:
        # fake the java metadata interface
        _md = self._metadata.deepcopy()

        def _nLevels(self_): return len(_md.get('levels', []))

        def _getLevel(self_, idx):
            r_lvl = object()
            _rl = _md.get('levels')[idx]
            r_lvl.getDownsample = lambda: _rl['downsample']
            r_lvl.getHeight = lambda: _rl['height']
            r_lvl.getWidth = lambda: _rl['width']
            return r_lvl

        md = object()
        md.nLevels = _nLevels.__get__(md, None)
        md.getLevel = _getLevel.__get__(md, None)
        return md


class _ProjectImageEntryMetadata(MutableMapping):
    """provides a python dict interface for image entry metadata"""

    def __init__(self, entry: DefaultProjectImageEntry) -> None:
        self._entry = entry

    def __setitem__(self, k: str, v: str) -> None:
        if not isinstance(k, str):
            raise TypeError(f"key must be of type `str` got `{type(k)}`")
        if not isinstance(v, str):
            raise TypeError(f"value must be of type `str` got `{type(v)}`")
        self._entry.putMetadataValue(String(str(k)), String(str(v)))

    def __delitem__(self, k: str) -> None:
        if not isinstance(k, str):
            raise TypeError(f"key must be of type `str` got `{type(k)}`")
        self._entry.removeMetadataValue(String(str(k)))

    def __getitem__(self, k: str) -> str:
        if not isinstance(k, str):
            raise TypeError(f"key must be of type `str` got `{type(k)}`")
        v = self._entry.getMetadataValue(String(str(k)))
        if v is None:
            raise KeyError(f"'{k}' not in metadata")
        return str(v)

    def __len__(self) -> int:
        return int(self._entry.getMetadataKeys().size())

    def __iter__(self) -> Iterator[str]:
        return iter(map(str, self._entry.getMetadataKeys()))

    def __contains__(self, item):
        return bool(self._entry.containsMetadata(String(str(item))))

    def clear(self) -> None:
        self._entry.clearMetadata()

    def __repr__(self):
        return f"<Metadata({repr(dict(self))})>"


class _ImageDataProperties(MutableMapping):
    """provides a python dict interface for image data properties"""

    def __init__(self, image_data: ImageData) -> None:
        self._image_data = image_data

    def __setitem__(self, k: str, v: Any) -> None:
        if not isinstance(k, str):
            raise TypeError(f"key must be of type `str` got `{type(k)}`")
        self._image_data.setProperty(String(k), v)

    def __delitem__(self, k: str) -> None:
        if not isinstance(k, str):
            raise TypeError(f"key must be of type `str` got `{type(k)}`")
        self._image_data.removeProperty(String(k))

    def __getitem__(self, k: str) -> Any:
        if not isinstance(k, str):
            raise TypeError(f"key must be of type `str` got `{type(k)}`")
        if k not in self:
            raise KeyError(f"'{k}' not in metadata")
        v = self._image_data.getProperty(String(k))
        return v

    def __contains__(self, item: Any) -> bool:
        if not isinstance(item, str):
            return False
        return bool(
            self._image_data.getProperties().containsKey(String(item))
        )

    def __len__(self) -> int:
        return int(self._image_data.getProperties().size())

    def __iter__(self) -> Iterator[str]:
        return iter(map(str, dict(self._image_data.getProperties())))

    def __repr__(self):
        return f"<Properties({repr(dict(self))})>"


# note: this could just be autogenerated by inspecting the ImageType
#   but it's better to be explicit so that all values are defined here
class QuPathImageType(str, Enum):
    """Enum representing image types"""
    java_enum: ImageType

    def __new__(cls, value: str, java_enum: ImageType):
        # noinspection PyArgumentList
        obj = super().__new__(cls, value)
        obj._value_ = value
        obj.java_enum = java_enum
        return obj

    @classmethod
    def from_java(cls, java_enum) -> 'QuPathImageType':
        """internal for converting from java to python"""
        for value in cls.__members__.values():
            if value.java_enum == java_enum:
                return value
        raise ValueError("unsupported java_enum")  # pragma: no cover

    # Brightfield image with hematoxylin and DAB stains.
    BRIGHTFIELD_H_DAB = ("Brightfield (H-DAB)", ImageType.BRIGHTFIELD_H_DAB)
    # Brightfield image with hematoxylin and eosin stains.
    BRIGHTFIELD_H_E = ("Brightfield (H&E)", ImageType.BRIGHTFIELD_H_E)
    # Brightfield image with any stains.
    BRIGHTFIELD_OTHER = ("Brightfield (other)", ImageType.BRIGHTFIELD_OTHER)
    # Fluorescence image.
    FLUORESCENCE = ("Fluorescence", ImageType.FLUORESCENCE)
    # Other image type, not covered by any of the alternatives above.
    OTHER = ("Other", ImageType.OTHER)
    # Image type has not been set.
    UNSET = ("Not set", ImageType.UNSET)


class QuPathProjectImageEntry(QuPathBase[DefaultProjectImageEntry]):

    def __init__(self, entry: DefaultProjectImageEntry) -> None:
        """Wrapper for qupath image entries

        this is normally not instantiated by the user
        """
        if not isinstance(entry, DefaultProjectImageEntry):
            raise ValueError("don't instantiate directly. use `QuPathProject.add_image`")
        super().__init__(entry)
        self._metadata = _ProjectImageEntryMetadata(entry)

    @cached_property
    def _image_data(self):
        try:
            with redirect(stdout=True, stderr=True):
                return self.java_object.readImageData()
        # from java land
        except IOException:  # pragma: no cover
            image_data_fn = self.entry_path / "data.qpdata"
            return PathIO.readImageData(
                File(str(image_data_fn)),
                None, None, BufferedImage
            )

    @cached_property
    def _properties(self):
        return _ImageDataProperties(self._image_data)

    @cached_property
    def _image_server(self):
        server = self._image_data.getServer()
        if not server:
            _log.warning("recovering readonly from server.json")
            server = _RecoveredReadOnlyImageServer(self.entry_path)
        return server

    @property
    def entry_id(self) -> str:
        """the unique image entry id"""
        return str(self.java_object.getID())

    @property
    def entry_path(self) -> Path:
        """path to the image directory"""
        return Path(str(self.java_object.getEntryPath().toString()))

    @property
    def image_name(self) -> str:
        """the image entry name"""
        return str(self.java_object.getImageName())

    @image_name.setter
    def image_name(self, name: str) -> None:
        self.java_object.setImageName(String(name))

    # remove until there's a good use case for this...
    # @property
    # def image_name_original(self) -> Optional[str]:
    #     """original name in case the user has changed the image name"""
    #     org_name = self.java_object.getOriginalImageName()
    #     return str(org_name) if org_name else None

    @property
    def image_type(self) -> QuPathImageType:
        """image type"""
        return QuPathImageType.from_java(self._image_data.getImageType())

    @image_type.setter
    def image_type(self, value: QuPathImageType) -> None:
        if not isinstance(value, QuPathImageType):
            raise TypeError("requires a QuPathImageType enum")
        self._image_data.setImageType(value.java_enum)

    @property
    def description(self) -> str:
        """free text describing the image"""
        text = self.java_object.getDescription()
        if text is None:
            return ""
        return str(text)

    @description.setter
    def description(self, text: str) -> None:
        self.java_object.setDescription(text)

    @property
    def width(self):
        return int(self._image_server.getWidth())

    @property
    def height(self):
        return int(self._image_server.getHeight())

    @property
    def num_channels(self):
        return int(self._image_server.nChannels())

    @property
    def num_z_slices(self):
        return int(self._image_server.nZSlices())

    @property
    def num_timepoints(self):
        return int(self._image_server.nTimepoints())

    @cached_property
    def downsample_levels(self) -> List[Dict[str, float]]:
        md = self._image_server.getMetadata()
        levels = []
        for level in range(int(md.nLevels())):
            resolution_level = md.getLevel(level)
            levels.append({
                'downsample': float(resolution_level.getDownsample()),
                'width': int(resolution_level.getWidth()),
                'height': int(resolution_level.getHeight()),
            })
        return levels

    @property
    def metadata(self) -> _ProjectImageEntryMetadata:
        """the metadata stored on the image as dict-like proxy"""
        return self._metadata

    @metadata.setter
    def metadata(self, value: dict) -> None:
        self._metadata.clear()
        self._metadata.update(value)

    @property
    def properties(self):
        """the properties stored in the image data as a dict-like proxy"""
        return self._properties

    @properties.setter
    def properties(self, value):
        self._properties.clear()
        self._properties.update(value)

    @cached_property
    def hierarchy(self) -> QuPathPathObjectHierarchy:
        """the image entry hierarchy. it contains all annotations"""
        try:
            return QuPathPathObjectHierarchy(self._image_data.getHierarchy())
        except OSError:
            _log.warning("could not open image data. loading annotation hierarchy from project.")
            return QuPathPathObjectHierarchy(self.java_object.readHierarchy())

    def __repr__(self):
        return f"<ImageEntry('{self.image_name}')>"

    @property
    def uri(self):
        """the image entry uri"""
        uris = self.java_object.getServerURIs()
        if len(uris) == 0:
            raise RuntimeError("no server")  # pragma: no cover
        elif len(uris) > 1:
            raise NotImplementedError("unsupported in paquo as of now")
        return str(uris[0].toString())

    def is_readable(self) -> bool:
        """check if the image file is readable"""
        concrete_path = Path(ImageProvider.path_from_uri(self.uri))
        return concrete_path.is_file()

    def is_changed(self) -> bool:
        """check if image_data is changed

        Raises
        ------
        IOError
            if image_data can't be read

        """
        return bool(self._image_data.isChanged())

    def save(self):
        """save image entry"""
        with redirect(stdout=True, stderr=True):
            if self.is_readable() and self.is_changed():
                self.java_object.saveImageData(self._image_data)
