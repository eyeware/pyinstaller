#-----------------------------------------------------------------------------
# Copyright (c) 2005-2015, PyInstaller Development Team.
#
# Distributed under the terms of the GNU General Public License with exception
# for distributing bootloader.
#
# The full license is in the file COPYING.txt, distributed with this software.
#-----------------------------------------------------------------------------


"""
This module contains classes that are available for the .spec files.

Spec file is generated by PyInstaller. The generated code from .spec file
is a way how PyInstaller does the dependency analysis and creates executable.
"""
import os
import shutil
import tempfile

from PyInstaller import is_win, is_darwin, HOMEPATH, PLATFORM
from PyInstaller.archive.writers import ZlibArchiveWriter, CArchiveWriter
from PyInstaller.building.utils import _check_guts_toc_mtime, _check_guts_toc, add_suffix_to_extensions, \
    checkCache, _check_path_overlap, _rmtree
from PyInstaller.compat import is_cygwin
from PyInstaller.depend import bindepend
from PyInstaller.depend.analysis import get_bootstrap_modules
from PyInstaller.depend.utils import is_path_to_egg
from PyInstaller.building.datastruct import TOC, Target, logger, _check_guts_eq
from PyInstaller.utils import misc
from PyInstaller.utils.misc import save_py_data_struct
from .. import log as logging

logger = logging.getLogger(__name__)

if is_win:
    from PyInstaller.utils.win32 import winmanifest, icon, versioninfo, winresource


class PYZ(Target):
    """
    Creates a ZlibArchive that contains all pure Python modules.
    """
    typ = 'PYZ'

    def __init__(self, toc_dict, name=None, cipher=None):
        """
        toc_dict
            toc_dict['toc']
                A TOC (Table of Contents), normally an Analysis.pure['toc']?
            toc_dict['code']
                A dict of module code objects from ModuleGraph.
        name
                A filename for the .pyz. Normally not needed, as the generated
                name will do fine.
        cipher
                The block cipher that will be used to encrypt Python bytecode.
        """

        from ..config import CONF
        Target.__init__(self)
        # TODO remove this attribute, PYZ items are compressed by default.
        self.compression_level = 0
        self.toc = toc_dict['toc']
        # Use code objects directly from ModuleGraph to speed up PyInstaller.
        self.code_dict = toc_dict['code']
        self.name = name
        if name is None:
            self.name = self.out[:-3] + 'pyz'
        # PyInstaller bootstrapping modules.
        self.dependencies = get_bootstrap_modules()
        # Bundle the crypto key.
        self.cipher = cipher
        if cipher:
            key_file = ('pyimod00_crypto_key',
                         os.path.join(CONF['workpath'], 'pyimod00_crypto_key.pyc'),
                         'PYMODULE')
            # Insert the key as the first module in the list. The key module contains
            # just variables and does not depend on other modules.
            self.dependencies.insert(0, key_file)
        # Compile the top-level modules so that they end up in the CArchive and can be
        # imported by the bootstrap script.
        self.dependencies = misc.compile_py_files(self.dependencies, CONF['workpath'])
        self.__postinit__()

    GUTS = (('name', _check_guts_eq),
            ('compression_level', _check_guts_eq),
            ('toc', _check_guts_toc),  # todo: pyc=1
            )

    def check_guts(self, last_build):
        if not os.path.exists(self.name):
            logger.info("Rebuilding %s because %s is missing",
                        self.outnm, os.path.basename(self.name))
            return True

        data = Target.get_guts(self, last_build)
        if not data:
            return True
        return False

    def assemble(self):
        logger.info("Building PYZ (ZlibArchive) %s", os.path.basename(self.out))
        pyz = ZlibArchiveWriter(code_dict=self.code_dict, cipher=self.cipher)
        # Do not bundle PyInstaller bootstrap modules into PYZ archive.
        toc = self.toc - self.dependencies
        pyz.build(self.name, toc)
        # FIXME compression level was dropped - remove it from the save_py_data_struct
        save_py_data_struct(self.out, (self.name, self.compression_level, self.toc))
        return 1


class PKG(Target):
    """
    Creates a CArchive. CArchive is the data structure that is embedded
    into the executable. This data structure allows to include various
    read-only data in a sigle-file deployment.
    """
    typ = 'PKG'
    xformdict = {'PYMODULE': 'm',
                 'PYSOURCE': 's',
                 'EXTENSION': 'b',
                 'PYZ': 'z',
                 'PKG': 'a',
                 'DATA': 'x',
                 'BINARY': 'b',
                 'ZIPFILE': 'Z',
                 'EXECUTABLE': 'b',
                 'DEPENDENCY': 'd'}

    def __init__(self, toc, name=None, cdict=None, exclude_binaries=0,
                 strip_binaries=False, upx_binaries=False):
        """
        toc
                A TOC (Table of Contents)
        name
                An optional filename for the PKG.
        cdict
                Dictionary that specifies compression by typecode. For Example,
                PYZ is left uncompressed so that it can be accessed inside the
                PKG. The default uses sensible values. If zlib is not available,
                no compression is used.
        exclude_binaries
                If True, EXTENSIONs and BINARYs will be left out of the PKG,
                and forwarded to its container (usually a COLLECT).
        strip_binaries
                If True, use 'strip' command to reduce the size of binary files.
        upx_binaries
        """
        Target.__init__(self)
        self.toc = toc
        self.cdict = cdict
        self.name = name
        self.exclude_binaries = exclude_binaries
        self.strip_binaries = strip_binaries
        self.upx_binaries = upx_binaries
        if name is None:
            self.name = self.out[:-3] + 'pkg'
        # This dict tells PyInstaller what items embedded in the executable should
        # be compressed.
        if self.cdict is None:
            self.cdict = {'EXTENSION': COMPRESSED,
                          'DATA': COMPRESSED,
                          'BINARY': COMPRESSED,
                          'EXECUTABLE': COMPRESSED,
                          'PYSOURCE': COMPRESSED,
                          'PYMODULE': COMPRESSED,
                          # Do not compress PYZ as a whole. Single modules are
                          # compressed when creating PYZ archive.
                          'PYZ': UNCOMPRESSED}
        self.__postinit__()

    GUTS = (('name', _check_guts_eq),
            ('cdict', _check_guts_eq),
            ('toc', _check_guts_toc_mtime),
            ('exclude_binaries', _check_guts_eq),
            ('strip_binaries', _check_guts_eq),
            ('upx_binaries', _check_guts_eq),
            )

    def check_guts(self, last_build):
        if not os.path.exists(self.name):
            logger.info("Rebuilding %s because %s is missing",
                        self.outnm, os.path.basename(self.name))
            return 1

        data = Target.get_guts(self, last_build)
        if not data:
            return True
        # todo: toc equal
        return False

    def assemble(self):
        logger.info("Building PKG (CArchive) %s", os.path.basename(self.name))
        trash = []
        mytoc = []
        seenInms = {}
        seenFnms = {}
        toc = add_suffix_to_extensions(self.toc)
        # 'inm'  - relative filename inside a CArchive
        # 'fnm'  - absolute filename as it is on the file system.
        for inm, fnm, typ in toc:
            # Ensure filename 'fnm' is not None or empty string. Otherwise
            # it will fail in case of 'typ' being type OPTION.
            if fnm and not os.path.isfile(fnm) and is_path_to_egg(fnm):
                # file is contained within python egg, it is added with the egg
                continue
            if typ in ('BINARY', 'EXTENSION', 'DEPENDENCY'):
                if self.exclude_binaries and typ != 'DEPENDENCY':
                    self.dependencies.append((inm, fnm, typ))
                else:
                    if typ == 'BINARY':
                        # Avoid importing the same binary extension twice. This might
                        # happen if they come from different sources (eg. once from
                        # binary dependence, and once from direct import).
                        if inm in seenInms:
                            logger.warn("Two binaries added with the same internal "
                                        "name. %s was placed at %s previously. "
                                        "Skipping %s." %
                                        (seenInms[inm], inm, fnm))
                            continue

                        # Warn if the same binary extension was included
                        # with multiple internal names
                        if fnm in seenFnms:
                            logger.warn("One binary added with two internal "
                                        "names. %s was placed at %s previously." %
                                        (fnm, seenFnms[fnm]))
                    seenInms[inm] = fnm
                    seenFnms[fnm] = inm

                    fnm = checkCache(fnm, strip=self.strip_binaries,
                                     upx=(self.upx_binaries and (is_win or is_cygwin)),
                                     dist_nm=inm)

                    mytoc.append((inm, fnm, self.cdict.get(typ, 0),
                                  self.xformdict.get(typ, 'b')))
            elif typ == 'OPTION':
                mytoc.append((inm, '', 0, 'o'))
            else:
                mytoc.append((inm, fnm, self.cdict.get(typ, 0), self.xformdict.get(typ, 'b')))

        # Bootloader has to know the name of Python library. Pass python libname to CArchive.
        pylib_name = os.path.basename(bindepend.get_python_library_path())
        archive = CArchiveWriter(pylib_name=pylib_name)

        archive.build(self.name, mytoc)
        save_py_data_struct(self.out,
                   (self.name, self.cdict, self.toc, self.exclude_binaries,
                    self.strip_binaries, self.upx_binaries))
        for item in trash:
            os.remove(item)
        return 1


class EXE(Target):
    """
    Creates the final executable of the frozen app.
    This bundles all necessary files together.
    """
    typ = 'EXECUTABLE'

    def __init__(self, *args, **kwargs):
        """
        args
                One or more arguments that are either TOCs Targets.
        kwargs
            Possible keywork arguments:

            console
                On Windows or OSX governs whether to use the console executable
                or the windowed executable. Always True on Linux/Unix (always
                console executable - it does not matter there).
            debug
                Setting to True gives you progress mesages from the executable
                (for console=False there will be annoying MessageBoxes on Windows).
            name
                The filename for the executable.
            exclude_binaries
                Forwarded to the PKG the EXE builds.
            icon
                Windows or OSX only. icon='myicon.ico' to use an icon file or
                icon='notepad.exe,0' to grab an icon resource.
            version
                Windows only. version='myversion.txt'. Use grab_version.py to get
                a version resource from an executable and then edit the output to
                create your own. (The syntax of version resources is so arcane
                that I wouldn't attempt to write one from scratch).
            uac_admin
                Windows only. Setting to True creates a Manifest with will request
                elevation upon application restart
            uac_uiaccess
                Windows only. Setting to True allows an elevated application to
                work with Remote Desktop
        """
        from ..config import CONF
        Target.__init__(self)

        # Available options for EXE in .spec files.
        self.exclude_binaries = kwargs.get('exclude_binaries', False)
        self.console = kwargs.get('console', True)
        self.debug = kwargs.get('debug', False)
        self.name = kwargs.get('name', None)
        self.icon = kwargs.get('icon', None)
        self.versrsrc = kwargs.get('version', None)
        self.manifest = kwargs.get('manifest', None)
        self.resources = kwargs.get('resources', [])
        self.strip = kwargs.get('strip', False)
        # If ``append_pkg`` is false, the archive will not be appended
        # to the exe, but copied beside it.
        self.append_pkg = kwargs.get('append_pkg', True)

        # On Windows allows the exe to request admin privileges.
        self.uac_admin = kwargs.get('uac_admin', False)
        self.uac_uiaccess = kwargs.get('uac_uiaccess', False)

        if CONF['hasUPX']:
           self.upx = kwargs.get('upx', False)
        else:
           self.upx = False

        # Old .spec format included in 'name' the path where to put created
        # app. New format includes only exename.
        #
        # Ignore fullpath in the 'name' and prepend DISTPATH or WORKPATH.
        # DISTPATH - onefile
        # WORKPATH - onedir
        if self.exclude_binaries:
            # onedir mode - create executable in WORKPATH.
            self.name = os.path.join(CONF['workpath'], os.path.basename(self.name))
        else:
            # onefile mode - create executable in DISTPATH.
            self.name = os.path.join(CONF['distpath'], os.path.basename(self.name))

        # Base name of the EXE file without .exe suffix.
        base_name = os.path.basename(self.name)
        if is_win or is_cygwin:
            base_name = os.path.splitext(base_name)[0]
        self.pkgname = base_name + '.pkg'

        self.toc = TOC()

        ### Pass option -S to Python interpreter to disable the 'site' module.
        # With this option PyInstaller  won't append user-specific sitedirs to
        # sys.path.
        args += ([('S', None, 'OPTION')],)

        for arg in args:
            if isinstance(arg, TOC):
                self.toc.extend(arg)
            elif isinstance(arg, Target):
                self.toc.append((os.path.basename(arg.name), arg.name, arg.typ))
                self.toc.extend(arg.dependencies)
            else:
                self.toc.extend(arg)

        if is_win:
            filename = os.path.join(CONF['workpath'], CONF['specnm'] + ".exe.manifest")
            self.manifest = winmanifest.create_manifest(filename, self.manifest,
                self.console, self.uac_admin, self.uac_uiaccess)
            self.toc.append((os.path.basename(self.name) + ".manifest", filename,
                'BINARY'))

        self.pkg = PKG(self.toc, cdict=kwargs.get('cdict', None),
                       exclude_binaries=self.exclude_binaries,
                       strip_binaries=self.strip, upx_binaries=self.upx,
                       )
        self.dependencies = self.pkg.dependencies
        self.__postinit__()

    GUTS = (('name', _check_guts_eq),
            ('console', _check_guts_eq),
            ('debug', _check_guts_eq),
            ('icon', _check_guts_eq),
            ('versrsrc', _check_guts_eq),
            ('resources', _check_guts_eq),
            ('strip', _check_guts_eq),
            ('upx', _check_guts_eq),
            ('mtm', None,),  # checked bellow
            )

    def check_guts(self, last_build):
        if not os.path.exists(self.name):
            logger.info("Rebuilding %s because %s missing",
                        self.outnm, os.path.basename(self.name))
            return 1
        if not self.append_pkg and not os.path.exists(self.pkgname):
            logger.info("Rebuilding because %s missing",
                        os.path.basename(self.pkgname))
            return 1

        data = Target.get_guts(self, last_build)
        if not data:
            return True

        icon, versrsrc, resources = data[3:6]
        if (versrsrc or resources) and not is_win:
            # todo: really ignore :-)
            logger.warn('ignoring version, manifest and resources, platform not capable')
        if icon and not (is_win or is_darwin):
            logger.warn('ignoring icon, platform not capable')

        mtm = data[-1]
        if mtm != misc.mtime(self.name):
            logger.info("Rebuilding %s because mtimes don't match", self.outnm)
            return True
        if mtm < misc.mtime(self.pkg.out):
            logger.info("Rebuilding %s because pkg is more recent", self.outnm)
            return True

        return False

    def _bootloader_file(self, exe):
        """
        Pick up the right bootloader file - debug, console, windowed.
        """
        # Having console/windowed bootolader makes sense only on Windows and
        # Mac OS X.
        if is_win or is_darwin:
            if not self.console:
                exe = exe + 'w'
        # There are two types of bootloaders:
        # run     - release, no verbose messages in console.
        # run_d   - contains verbose messages in console.
        if self.debug:
            exe = exe + '_d'
        bootloader_file = os.path.join(HOMEPATH, 'PyInstaller', 'bootloader', PLATFORM, exe)
        logger.info('Bootloader %s' % bootloader_file)
        return bootloader_file

    def assemble(self):
        logger.info("Building EXE from %s", os.path.basename(self.out))
        trash = []
        if not os.path.exists(os.path.dirname(self.name)):
            os.makedirs(os.path.dirname(self.name))
        outf = open(self.name, 'wb')
        exe = self._bootloader_file('run')
        if is_win or is_cygwin:
            exe = exe + '.exe'

        if not os.path.exists(exe):
            raise SystemExit(_MISSING_BOOTLOADER_ERRORMSG)

        if is_win and not self.exclude_binaries:
            # Windows and onefile mode - embed manifest into exe.
            logger.info('Onefile Mode - Embedding Manifest into EXE file')
            tmpnm = tempfile.mktemp()
            shutil.copy2(exe, tmpnm)
            os.chmod(tmpnm, 0o755)

            # In onefile mode, dependencies in the onefile manifest
            # refer to files that are about to be unpacked when the exe
            # is run. The Windows DLL loader doesn't know that and
            # refuses to run the exe at all. Since the .exe does not in
            # fact depend on those, and the actual manifest will be used
            # later when an activation context is created, all
            # dependencies are removed from the embedded manifest.
            self.manifest.dependentAssemblies = []
            self.manifest.update_resources(tmpnm, [1]) # 1 for executable
            trash.append(tmpnm)
            exe = tmpnm

        if is_win and (self.icon or self.versrsrc or self.resources):
            tmpnm = tempfile.mktemp()
            shutil.copy2(exe, tmpnm)
            os.chmod(tmpnm, 0o755)
            if self.icon:
                icon.CopyIcons(tmpnm, self.icon)
            if self.versrsrc:
                versioninfo.SetVersion(tmpnm, self.versrsrc)
            for res in self.resources:
                res = res.split(",")
                for i in range(1, len(res)):
                    try:
                        res[i] = int(res[i])
                    except ValueError:
                        pass
                resfile = res[0]
                restype = resname = reslang = None
                if len(res) > 1:
                    restype = res[1]
                if len(res) > 2:
                    resname = res[2]
                if len(res) > 3:
                    reslang = res[3]
                try:
                    winresource.UpdateResourcesFromResFile(tmpnm, resfile,
                                                        [restype or "*"],
                                                        [resname or "*"],
                                                        [reslang or "*"])
                except winresource.pywintypes.error as exc:
                    if exc.args[0] != winresource.ERROR_BAD_EXE_FORMAT:
                        logger.exception(exc)
                        continue
                    if not restype or not resname:
                        logger.error("resource type and/or name not specified")
                        continue
                    if "*" in (restype, resname):
                        logger.error("no wildcards allowed for resource type "
                                     "and name when source file does not "
                                     "contain resources")
                        continue
                    try:
                        winresource.UpdateResourcesFromDataFile(tmpnm,
                                                             resfile,
                                                             restype,
                                                             [resname],
                                                             [reslang or 0])
                    except winresource.pywintypes.error as exc:
                        logger.exception(exc)
            trash.append(tmpnm)
            exe = tmpnm
        exe = checkCache(exe, strip=self.strip, upx=self.upx)
        self.copy(exe, outf)
        if self.append_pkg:
            logger.info("Appending archive to EXE %s", self.name)
            self.copy(self.pkg.name, outf)
        else:
            logger.info("Copying archive to %s", self.pkgname)
            shutil.copy2(self.pkg.name, self.pkgname)
        outf.close()

        if is_darwin:
            # Fix Mach-O header for codesigning on OS X.
            logger.info("Fixing EXE for code signing %s", self.name)
            from PyInstaller.utils import osxutils
            osxutils.fix_exe_for_code_signing(self.name)
            pass

        os.chmod(self.name, 0o755)
        guts = (self.name, self.console, self.debug, self.icon,
                self.versrsrc, self.resources, self.strip, self.upx,
                misc.mtime(self.name))
        assert len(guts) == len(self.GUTS)
        save_py_data_struct(self.out, guts)
        for item in trash:
            os.remove(item)
        return 1

    def copy(self, fnm, outf):
        inf = open(fnm, 'rb')
        while 1:
            data = inf.read(64 * 1024)
            if not data:
                break
            outf.write(data)


class DLL(EXE):
    # TODO This feature was dropped and I think it could be removed.
    """
    On Windows, this provides support for doing in-process COM servers. It is not
    generalized. However, embedders can follow the same model to build a special
    purpose process DLL so the Python support in their app is hidden. You will
    need to write your own dll.
    """
    def assemble(self):
        logger.info("Building DLL %s", os.path.basename(self.out))
        outf = open(self.name, 'wb')
        dll = self._bootloader_file('inprocsrvr') + '.dll'
        if not os.path.exists(dll):
            raise SystemExit(_MISSING_BOOTLOADER_ERRORMSG)
        self.copy(dll, outf)
        self.copy(self.pkg.name, outf)
        outf.close()
        os.chmod(self.name, 0o755)
        save_py_data_struct(self.out,
                   (self.name, self.console, self.debug, self.icon,
                    self.versrsrc, self.manifest, self.resources, self.strip, self.upx, misc.mtime(self.name)))
        return 1


class COLLECT(Target):
    """
    In one-dir mode creates the output folder with all necessary files.
    """
    def __init__(self, *args, **kws):
        """
        args
                One or more arguments that are either TOCs Targets.
        kws
            Possible keywork arguments:

                name
                    The name of the directory to be built.
        """
        from ..config import CONF
        Target.__init__(self)
        self.strip_binaries = kws.get('strip', False)

        if CONF['hasUPX']:
           self.upx_binaries = kws.get('upx', False)
        else:
           self.upx_binaries = False

        self.name = kws.get('name')
        # Old .spec format included in 'name' the path where to collect files
        # for the created app.
        # app. New format includes only directory name.
        #
        # The 'name' directory is created in DISTPATH and necessary files are
        # then collected to this directory.
        self.name = os.path.join(CONF['distpath'], os.path.basename(self.name))

        self.toc = TOC()
        for arg in args:
            if isinstance(arg, TOC):
                self.toc.extend(arg)
            elif isinstance(arg, Target):
                self.toc.append((os.path.basename(arg.name), arg.name, arg.typ))
                if isinstance(arg, EXE):
                    for tocnm, fnm, typ in arg.toc:
                        if tocnm == os.path.basename(arg.name) + ".manifest":
                            self.toc.append((tocnm, fnm, typ))
                    if not arg.append_pkg:
                        self.toc.append((os.path.basename(arg.pkgname), arg.pkgname, 'PKG'))
                self.toc.extend(arg.dependencies)
            else:
                self.toc.extend(arg)
        self.__postinit__()

    GUTS = (('name', _check_guts_eq),
            ('strip_binaries', _check_guts_eq),
            ('upx_binaries', _check_guts_eq),
            ('toc', _check_guts_eq),  # additional check below
            )

    def check_guts(self, last_build):
        # COLLECT always needs to be executed, since it will clean the output
        # directory anyway to make sure there is no existing cruft accumulating
        return 1

    def assemble(self):
        if _check_path_overlap(self.name) and os.path.isdir(self.name):
            _rmtree(self.name)
        logger.info("Building COLLECT %s", os.path.basename(self.out))
        os.makedirs(self.name)
        toc = add_suffix_to_extensions(self.toc)
        for inm, fnm, typ in toc:
            if not os.path.exists(fnm) or not os.path.isfile(fnm) and is_path_to_egg(fnm):
                # file is contained within python egg, it is added with the egg
                continue
            if os.pardir in os.path.normpath(inm) or os.path.isabs(inm):
                raise SystemExit('Security-Alert: try to store file outside '
                                 'of dist-directory. Aborting. %r' % inm)
            tofnm = os.path.join(self.name, inm)
            todir = os.path.dirname(tofnm)
            if not os.path.exists(todir):
                os.makedirs(todir)
            if typ in ('EXTENSION', 'BINARY'):
                fnm = checkCache(fnm, strip=self.strip_binaries,
                                 upx=(self.upx_binaries and (is_win or is_cygwin)),
                                 dist_nm=inm)
            if typ != 'DEPENDENCY':
                shutil.copy(fnm, tofnm)
                try:
                    shutil.copystat(fnm, tofnm)
                except OSError:
                    logger.warn("failed to copy flags of %s", fnm)
            if typ in ('EXTENSION', 'BINARY'):
                os.chmod(tofnm, 0o755)
        save_py_data_struct(self.out,
                 (self.name, self.strip_binaries, self.upx_binaries, self.toc))
        return 1


class MERGE(object):
    """
    Merge repeated dependencies from other executables into the first
    execuable. Data and binary files are then present only once and some
    disk space is thus reduced.
    """
    def __init__(self, *args):
        """
        Repeated dependencies are then present only once in the first
        executable in the 'args' list. Other executables depend on the
        first one. Other executables have to extract necessary files
        from the first executable.

        args  dependencies in a list of (Analysis, id, filename) tuples.
              Replace id with the correct filename.
        """
        # The first Analysis object with all dependencies.
        # Any item from the first executable cannot be removed.
        self._main = None

        self._dependencies = {}

        self._id_to_path = {}
        for _, i, p in args:
            self._id_to_path[i] = p

        # Get the longest common path
        self._common_prefix = os.path.dirname(os.path.commonprefix([os.path.abspath(a.scripts[-1][1]) for a, _, _ in args]))
        if self._common_prefix[-1] != os.sep:
            self._common_prefix += os.sep
        logger.info("Common prefix: %s", self._common_prefix)

        self._merge_dependencies(args)

    def _merge_dependencies(self, args):
        """
        Filter shared dependencies to be only in first executable.
        """
        for analysis, _, _ in args:
            path = os.path.abspath(analysis.scripts[-1][1]).replace(self._common_prefix, "", 1)
            path = os.path.splitext(path)[0]
            if path in self._id_to_path:
                path = self._id_to_path[path]
            self._set_dependencies(analysis, path)

    def _set_dependencies(self, analysis, path):
        """
        Synchronize the Analysis result with the needed dependencies.
        """
        for toc in (analysis.binaries, analysis.datas):
            for i, tpl in enumerate(toc):
                if not tpl[1] in self._dependencies:
                    logger.debug("Adding dependency %s located in %s" % (tpl[1], path))
                    self._dependencies[tpl[1]] = path
                else:
                    dep_path = self._get_relative_path(path, self._dependencies[tpl[1]])
                    logger.debug("Referencing %s to be a dependecy for %s, located in %s" % (tpl[1], path, dep_path))
                    analysis.dependencies.append((":".join((dep_path, tpl[0])), tpl[1], "DEPENDENCY"))
                    toc[i] = (None, None, None)
            # Clean the list
            toc[:] = [tpl for tpl in toc if tpl != (None, None, None)]

    # TODO move this function to PyInstaller.compat module (probably improve
    #      function compat.relpath()
    # TODO use os.path.relpath instead
    def _get_relative_path(self, startpath, topath):
        start = startpath.split(os.sep)[:-1]
        start = ['..'] * len(start)
        if start:
            start.append(topath)
            return os.sep.join(start)
        else:
            return topath


UNCOMPRESSED = 0
COMPRESSED = 1

_MISSING_BOOTLOADER_ERRORMSG = """
Fatal error: PyInstaller does not include a pre-compiled bootloader for your
platform. See <http://pythonhosted.org/PyInstaller/#building-the-bootloader>
for more details and instructions how to build the bootloader.
"""