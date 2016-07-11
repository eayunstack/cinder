# Copyright (C) 2012 Hewlett-Packard Development Company, L.P.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""
Tests for Backup code.

"""

import ddt
import tempfile

import mock
from oslo.config import cfg

from cinder.backup import manager
from cinder import context
from cinder import db
from cinder import exception
from cinder.openstack.common import importutils
from cinder.openstack.common import log as logging
from cinder.openstack.common import timeutils
from cinder import test
from cinder.tests.backup.fake_service_with_verify import\
    get_backup_driver
from cinder.volume.drivers import lvm


CONF = cfg.CONF

LOG = logging.getLogger(__name__)


class FakeBackupException(Exception):
    pass


class BaseBackupTest(test.TestCase):
    def setUp(self):
        super(BaseBackupTest, self).setUp()
        vol_tmpdir = tempfile.mkdtemp()
        self.flags(volumes_dir=vol_tmpdir)
        with mock.patch("osprofiler.profiler.trace_cls") as mock_trace_cls:
            side_effect = lambda value: value
            mock_decorator = mock.MagicMock(side_effect=side_effect)
            mock_trace_cls.return_value = mock_decorator
            self.backup_mgr = \
                importutils.import_object(CONF.backup_manager)
        self.backup_mgr.host = 'testhost'
        self.ctxt = context.get_admin_context()
        self.backup_mgr.driver.set_initialized()

    def _create_backup_db_entry(self, volume_id=1, display_name='test_backup',
                                display_description='this is a test backup',
                                container='volumebackups',
                                status='creating',
                                size=1,
                                object_count=0,
                                project_id='fake',
                                temp_volume_id=None,
                                temp_snapshot_id=None):
        """Create a backup entry in the DB.

        Return the entry ID
        """
        backup = {}
        backup['volume_id'] = volume_id
        backup['user_id'] = 'fake'
        backup['project_id'] = project_id
        backup['host'] = 'testhost'
        backup['availability_zone'] = '1'
        backup['display_name'] = display_name
        backup['display_description'] = display_description
        backup['container'] = container
        backup['status'] = status
        backup['fail_reason'] = ''
        backup['service'] = CONF.backup_driver
        backup['size'] = size
        backup['object_count'] = object_count
        backup['temp_volume_id'] = temp_volume_id
        backup['temp_snapshot_id'] = temp_snapshot_id
        return db.backup_create(self.ctxt, backup)['id']

    def _create_volume_db_entry(self, display_name='test_volume',
                                display_description='this is a test volume',
                                status='backing-up',
                                previous_status='available',
                                size=1):
        """Create a volume entry in the DB.

        Return the entry ID
        """
        vol = {}
        vol['size'] = size
        vol['host'] = 'testhost'
        vol['user_id'] = 'fake'
        vol['project_id'] = 'fake'
        vol['status'] = status
        vol['display_name'] = display_name
        vol['display_description'] = display_description
        vol['attach_status'] = 'detached'
        vol['availability_zone'] = '1'
        vol['previous_status'] = previous_status
        return db.volume_create(self.ctxt, vol)['id']

    def _create_snapshot_db_entry(self, display_name='test_snapshot',
                                  display_description='test snapshot',
                                  status='available',
                                  size=1,
                                  volume_id='1',
                                  provider_location=None):
        """Create a snapshot entry in the DB.

        Return the entry ID.
        """
        snap = {}
        snap['size'] = size
        snap['host'] = 'testhost'
        snap['user_id'] = 'fake'
        snap['project_id'] = 'fake'
        snap['status'] = status
        snap['display_name'] = display_name
        snap['display_description'] = display_description
        snap['volume_id'] = volume_id
        snap['cgsnapshot_id'] = None
        snap['volume_size'] = size
        snap['provider_location'] = provider_location
        return db.snapshot_create(self.ctxt, snap)['id']

    def _create_volume_attach(self, volume_id):
        values = {'volume_id': volume_id,
                  'attach_status': 'attached', }
        attachment = db.volume_attach(self.ctxt, values)
        db.volume_attached(self.ctxt, attachment['id'], None, 'testhost',
                           '/dev/vd0')

    def _create_exported_record_entry(self, vol_size=1):
        """Create backup metadata export entry."""
        vol_id = self._create_volume_db_entry(status='available',
                                              size=vol_size)
        backup_id = self._create_backup_db_entry(status='available',
                                                 volume_id=vol_id)

        export = self.backup_mgr.export_record(self.ctxt, backup_id)
        return export

    def _create_export_record_db_entry(self,
                                       volume_id='0000',
                                       status='creating',
                                       project_id='fake'):
        """Create a backup entry in the DB.

        Return the entry ID
        """
        backup = {}
        backup['volume_id'] = volume_id
        backup['user_id'] = 'fake'
        backup['project_id'] = project_id
        backup['status'] = status
        return db.backup_create(self.ctxt, backup)['id']


@ddt.ddt
class BackupTestCase(BaseBackupTest):
    """Test Case for backups."""

    @mock.patch.object(lvm.LVMVolumeDriver, 'delete_snapshot')
    @mock.patch.object(lvm.LVMVolumeDriver, 'delete_volume')
    def test_init_host(self, mock_delete_volume, mock_delete_snapshot):
        """Make sure stuck volumes and backups are reset to correct
        states when backup_manager.init_host() is called
        """
        vol1_id = self._create_volume_db_entry()
        self._create_volume_attach(vol1_id)
        db.volume_update(self.ctxt, vol1_id, {'status': 'backing-up'})
        vol2_id = self._create_volume_db_entry()
        self._create_volume_attach(vol2_id)
        db.volume_update(self.ctxt, vol2_id, {'status': 'restoring-backup'})
        vol3_id = self._create_volume_db_entry()
        db.volume_update(self.ctxt, vol3_id, {'status': 'available'})
        vol4_id = self._create_volume_db_entry()
        db.volume_update(self.ctxt, vol4_id, {'status': 'backing-up'})
        temp_vol_id = self._create_volume_db_entry()
        db.volume_update(self.ctxt, temp_vol_id, {'status': 'available'})
        vol5_id = self._create_volume_db_entry()
        db.volume_update(self.ctxt, vol5_id, {'status': 'backing-up'})
        temp_snap_id = self._create_snapshot_db_entry()
        db.snapshot_update(self.ctxt, temp_snap_id, {'status': 'available'})
        vol6_id = self._create_volume_db_entry()
        db.volume_update(self.ctxt, vol6_id, {'status': 'restoring-backup'})

        backup1_id = self._create_backup_db_entry(status='creating',
                                                  volume_id=vol1_id)
        backup2_id = self._create_backup_db_entry(status='restoring',
                                                  volume_id=vol2_id)
        backup3_id = self._create_backup_db_entry(status='deleting',
                                                  volume_id=vol3_id)
        self._create_backup_db_entry(status='creating',
                                     volume_id=vol4_id,
                                     temp_volume_id=temp_vol_id)
        self._create_backup_db_entry(status='creating',
                                     volume_id=vol5_id,
                                     temp_snapshot_id=temp_snap_id)

        self.backup_mgr.init_host()

        vol1 = db.volume_get(self.ctxt, vol1_id)
        self.assertEqual(vol1['status'], 'available')
        vol2 = db.volume_get(self.ctxt, vol2_id)
        self.assertEqual('error_restoring', vol2['status'])
        vol3 = db.volume_get(self.ctxt, vol3_id)
        self.assertEqual('available', vol3['status'])
        vol4 = db.volume_get(self.ctxt, vol4_id)
        self.assertEqual('available', vol4['status'])
        vol5 = db.volume_get(self.ctxt, vol5_id)
        self.assertEqual('available', vol5['status'])
        vol6 = db.volume_get(self.ctxt, vol6_id)
        self.assertEqual('error_restoring', vol6['status'])

        backup1 = db.backup_get(self.ctxt, backup1_id)
        self.assertEqual(backup1['status'], 'error')
        backup2 = db.backup_get(self.ctxt, backup2_id)
        self.assertEqual(backup2['status'], 'available')
        self.assertRaises(exception.BackupNotFound,
                          db.backup_get,
                          self.ctxt,
                          backup3_id)

        self.assertTrue(mock_delete_volume.called)
        self.assertTrue(mock_delete_snapshot.called)

    @mock.patch.object(manager.BackupManager,
                       '_cleanup_incomplete_backup_operations')
    @mock.patch.object(manager.BackupManager, '_init_volume_driver')
    def test_init_host_handles_exception(
            self, mock_init_driver, mock_cleanup):
        """Test that exception in cleanup is handled."""

        mock_cleanup.side_effect = [Exception]

        self.assertIsNone(self.backup_mgr.init_host())

    @mock.patch.object(manager.BackupManager,
                       '_cleanup_temp_volumes_snapshots_for_one_backup')
    @mock.patch.object(manager.BackupManager, '_cleanup_one_backup')
    @mock.patch.object(db, 'backup_get_all_by_host')
    @mock.patch.object(manager.BackupManager, '_cleanup_one_volume')
    @mock.patch.object(db, 'volume_get_all_by_host')
    def test_cleanup_incomplete_backup_operations_with_exceptions(
            self, mock_volume_get_by_host, mock_volume_cleanup,
            mock_backup_get_by_host, mock_backup_cleanup,
            mock_temp_cleanup):
        """Test cleanup resilience in the face of exceptions."""

        fake_volume_list = [{'id': 'vol1'}, {'id': 'vol2'}]
        mock_volume_get_by_host.return_value = fake_volume_list

        mock_volume_cleanup.side_effect = [Exception]

        fake_backup_list = [{'id': 'bkup1'}, {'id': 'bkup2'}, {'id': 'bkup3'}]
        mock_backup_get_by_host.return_value = fake_backup_list

        mock_backup_cleanup.side_effect = [Exception]

        mock_temp_cleanup.side_effect = [Exception]

        self.assertIsNone(
            self.backup_mgr._cleanup_incomplete_backup_operations(
                self.ctxt))

        self.assertEqual(len(fake_volume_list), mock_volume_cleanup.call_count)
        self.assertEqual(len(fake_backup_list), mock_backup_cleanup.call_count)
        self.assertEqual(len(fake_backup_list), mock_temp_cleanup.call_count)

    @mock.patch.object(manager.BackupManager, '_get_manager')
    def test_cleanup_one_backing_up_volume(self, mock_get_manager):
        """Test cleanup_one_volume for volume status 'backing-up'."""

        mock_get_manager.return_value = 'fake_manager'

        volume_id = self._create_volume_db_entry(status='backing-up',
                                                 previous_status='available')
        volume = db.volume_get(self.ctxt, volume_id)

        self.backup_mgr._cleanup_one_volume(self.ctxt, volume)

        volume = db.volume_get(self.ctxt, volume_id)
        self.assertEqual('available', volume['status'])

    @mock.patch.object(manager.BackupManager, '_get_manager')
    def test_cleanup_one_restoring_backup_volume(self, mock_get_manager):
        """Test cleanup_one_volume for volume status 'restoring-backup'."""

        mock_get_manager.return_value = 'fake_manager'

        volume_id = self._create_volume_db_entry(status='restoring-backup')
        volume = db.volume_get(self.ctxt, volume_id)

        self.backup_mgr._cleanup_one_volume(self.ctxt, volume)

        volume = db.volume_get(self.ctxt, volume_id)
        self.assertEqual('error_restoring', volume['status'])

    def test_cleanup_one_creating_backup(self):
        """Test cleanup_one_backup for volume status 'creating'."""

        backup_id = self._create_backup_db_entry(status='creating')
        backup = db.backup_get(self.ctxt, backup_id)

        self.backup_mgr._cleanup_one_backup(self.ctxt, backup)

        self.assertEqual('error', backup['status'])

    def test_cleanup_one_restoring_backup(self):
        """Test cleanup_one_backup for volume status 'restoring'."""

        backup_id = self._create_backup_db_entry(status='restoring')
        backup = db.backup_get(self.ctxt, backup_id)

        self.backup_mgr._cleanup_one_backup(self.ctxt, backup)

        self.assertEqual('available', backup['status'])

    def test_cleanup_one_deleting_backup(self):
        """Test cleanup_one_backup for volume status 'deleting'."""

        backup_id = self._create_backup_db_entry(status='deleting')
        backup = db.backup_get(self.ctxt, backup_id)

        self.backup_mgr._cleanup_one_backup(self.ctxt, backup)

        self.assertRaises(exception.BackupNotFound,
                          db.backup_get,
                          self.ctxt,
                          backup['id'])

    @mock.patch.object(manager, 'LOG')
    def test_detach_all_attachments_handles_exceptions(self, mock_log):
        """Test detach_all_attachments with exceptions."""

        mock_volume_mgr = mock.Mock()
        mock_detach_volume = mock_volume_mgr.detach_volume
        mock_detach_volume.side_effect = [Exception]

        fake_attachments = [
            {
                'id': 'attachment1',
                'attached_host': 'testhost',
                'instance_uuid': None,
            },
            {
                'id': 'attachment2',
                'attached_host': 'testhost',
                'instance_uuid': None,
            }
        ]
        fake_volume = {
            'id': 'fake_volume_id',
            'volume_attachment': fake_attachments
        }

        self.backup_mgr._detach_all_attachments(self.ctxt,
                                                mock_volume_mgr,
                                                fake_volume)

        self.assertEqual(len(fake_attachments), mock_log.exception.call_count)

    @mock.patch.object(db, 'volume_get')
    @ddt.data(KeyError, exception.VolumeNotFound)
    def test_cleanup_temp_volumes_snapshots_for_one_backup_volume_not_found(
            self, err, mock_volume_get):
        """Ensure we handle missing volume for a backup."""

        mock_volume_get.side_effect = [err]

        backup_id = self._create_backup_db_entry(status='creating')
        backup = db.backup_get(self.ctxt, backup_id)

        self.assertIsNone(
            self.backup_mgr._cleanup_temp_volumes_snapshots_for_one_backup(
                self.ctxt,
                backup))

    @mock.patch.object(lvm.LVMVolumeDriver, 'delete_snapshot')
    def test_cleanup_temp_snapshot_for_one_backup_not_found(
            self, mock_delete_snapshot):
        """Ensure we handle missing temp snapshot for a backup."""

        vol1_id = self._create_volume_db_entry()
        self._create_volume_attach(vol1_id)
        db.volume_update(self.ctxt, vol1_id, {'status': 'backing-up'})
        backup_id = self._create_backup_db_entry(status='error',
                                                 volume_id=vol1_id,
                                                 temp_snapshot_id='fake')
        backup = db.backup_get(self.ctxt, backup_id)

        self.assertIsNone(
            self.backup_mgr._cleanup_temp_volumes_snapshots_for_one_backup(
                self.ctxt,
                backup))

        self.assertFalse(mock_delete_snapshot.called)
        self.assertIsNone(backup['temp_snapshot_id'])

        db.backup_destroy(self.ctxt, backup_id)
        db.volume_destroy(self.ctxt, vol1_id)

    @mock.patch.object(lvm.LVMVolumeDriver, 'delete_volume')
    def test_cleanup_temp_volume_for_one_backup_not_found(
            self, mock_delete_volume):
        """Ensure we handle missing temp volume for a backup."""

        vol1_id = self._create_volume_db_entry()
        self._create_volume_attach(vol1_id)
        db.volume_update(self.ctxt, vol1_id, {'status': 'backing-up'})
        backup_id = self._create_backup_db_entry(status='error',
                                                 volume_id=vol1_id,
                                                 temp_volume_id='fake')
        backup = db.backup_get(self.ctxt, backup_id)

        self.assertIsNone(
            self.backup_mgr._cleanup_temp_volumes_snapshots_for_one_backup(
                self.ctxt,
                backup))

        self.assertFalse(mock_delete_volume.called)
        self.assertIsNone(backup['temp_volume_id'])

        db.backup_destroy(self.ctxt, backup_id)
        db.volume_destroy(self.ctxt, vol1_id)

    def test_create_backup_with_bad_volume_status(self):
        """Test creating a backup from a volume with a bad status."""
        vol_id = self._create_volume_db_entry(status='restoring', size=1)
        backup_id = self._create_backup_db_entry(volume_id=vol_id)
        self.assertRaises(exception.InvalidVolume,
                          self.backup_mgr.create_backup,
                          self.ctxt,
                          backup_id)

    def test_create_backup_with_bad_backup_status(self):
        """Test creating a backup with a backup with a bad status."""
        vol_id = self._create_volume_db_entry(size=1)
        backup_id = self._create_backup_db_entry(status='available',
                                                 volume_id=vol_id)
        self.assertRaises(exception.InvalidBackup,
                          self.backup_mgr.create_backup,
                          self.ctxt,
                          backup_id)

    @mock.patch('%s.%s' % (CONF.volume_driver, 'backup_volume'))
    def test_create_backup_with_error(self, _mock_volume_backup):
        """Test error handling when error occurs during backup creation."""
        vol_id = self._create_volume_db_entry(size=1)
        backup_id = self._create_backup_db_entry(volume_id=vol_id)

        _mock_volume_backup.side_effect = FakeBackupException('fake')
        self.assertRaises(FakeBackupException,
                          self.backup_mgr.create_backup,
                          self.ctxt,
                          backup_id)
        vol = db.volume_get(self.ctxt, vol_id)
        self.assertEqual('available', vol['status'])
        self.assertEqual('error_backing-up', vol['previous_status'])
        backup = db.backup_get(self.ctxt, backup_id)
        self.assertEqual('error', backup['status'])
        self.assertTrue(_mock_volume_backup.called)

    @mock.patch('%s.%s' % (CONF.volume_driver, 'backup_volume'))
    def test_create_backup(self, _mock_volume_backup):
        """Test normal backup creation."""
        vol_size = 1
        vol_id = self._create_volume_db_entry(size=vol_size)
        backup_id = self._create_backup_db_entry(volume_id=vol_id)

        self.backup_mgr.create_backup(self.ctxt, backup_id)
        vol = db.volume_get(self.ctxt, vol_id)
        self.assertEqual('available', vol['status'])
        self.assertEqual('backing-up', vol['previous_status'])
        backup = db.backup_get(self.ctxt, backup_id)
        self.assertEqual('available', backup['status'])
        self.assertEqual(backup['size'], vol_size)
        self.assertTrue(_mock_volume_backup.called)

    def test_restore_backup_with_bad_volume_status(self):
        """Test error handling when restoring a backup to a volume
        with a bad status.
        """
        vol_id = self._create_volume_db_entry(status='available', size=1)
        backup_id = self._create_backup_db_entry(volume_id=vol_id)
        self.assertRaises(exception.InvalidVolume,
                          self.backup_mgr.restore_backup,
                          self.ctxt,
                          backup_id,
                          vol_id)
        backup = db.backup_get(self.ctxt, backup_id)
        self.assertEqual(backup['status'], 'available')

    def test_restore_backup_with_bad_backup_status(self):
        """Test error handling when restoring a backup with a backup
        with a bad status.
        """
        vol_id = self._create_volume_db_entry(status='restoring-backup',
                                              size=1)
        backup_id = self._create_backup_db_entry(status='available',
                                                 volume_id=vol_id)
        self.assertRaises(exception.InvalidBackup,
                          self.backup_mgr.restore_backup,
                          self.ctxt,
                          backup_id,
                          vol_id)
        vol = db.volume_get(self.ctxt, vol_id)
        self.assertEqual(vol['status'], 'error')
        backup = db.backup_get(self.ctxt, backup_id)
        self.assertEqual(backup['status'], 'error')

    @mock.patch('%s.%s' % (CONF.volume_driver, 'restore_backup'))
    def test_restore_backup_with_driver_error(self, _mock_volume_restore):
        """Test error handling when an error occurs during backup restore."""
        vol_id = self._create_volume_db_entry(status='restoring-backup',
                                              size=1)
        backup_id = self._create_backup_db_entry(status='restoring',
                                                 volume_id=vol_id)

        _mock_volume_restore.side_effect = FakeBackupException('fake')
        self.assertRaises(FakeBackupException,
                          self.backup_mgr.restore_backup,
                          self.ctxt,
                          backup_id,
                          vol_id)
        vol = db.volume_get(self.ctxt, vol_id)
        self.assertEqual(vol['status'], 'error_restoring')
        backup = db.backup_get(self.ctxt, backup_id)
        self.assertEqual(backup['status'], 'available')
        self.assertTrue(_mock_volume_restore.called)

    def test_restore_backup_with_bad_service(self):
        """Test error handling when attempting a restore of a backup
        with a different service to that used to create the backup.
        """
        vol_id = self._create_volume_db_entry(status='restoring-backup',
                                              size=1)
        backup_id = self._create_backup_db_entry(status='restoring',
                                                 volume_id=vol_id)

        service = 'cinder.tests.backup.bad_service'
        db.backup_update(self.ctxt, backup_id, {'service': service})
        self.assertRaises(exception.InvalidBackup,
                          self.backup_mgr.restore_backup,
                          self.ctxt,
                          backup_id,
                          vol_id)
        vol = db.volume_get(self.ctxt, vol_id)
        self.assertEqual(vol['status'], 'error')
        backup = db.backup_get(self.ctxt, backup_id)
        self.assertEqual(backup['status'], 'available')

    @mock.patch('%s.%s' % (CONF.volume_driver, 'restore_backup'))
    def test_restore_backup(self, _mock_volume_restore):
        """Test normal backup restoration."""
        vol_size = 1
        vol_id = self._create_volume_db_entry(status='restoring-backup',
                                              size=vol_size)
        backup_id = self._create_backup_db_entry(status='restoring',
                                                 volume_id=vol_id)

        self.backup_mgr.restore_backup(self.ctxt, backup_id, vol_id)
        vol = db.volume_get(self.ctxt, vol_id)
        self.assertEqual(vol['status'], 'available')
        backup = db.backup_get(self.ctxt, backup_id)
        self.assertEqual(backup['status'], 'available')
        self.assertTrue(_mock_volume_restore.called)

    def test_delete_backup_with_bad_backup_status(self):
        """Test error handling when deleting a backup with a backup
        with a bad status.
        """
        vol_id = self._create_volume_db_entry(size=1)
        backup_id = self._create_backup_db_entry(status='available',
                                                 volume_id=vol_id)
        self.assertRaises(exception.InvalidBackup,
                          self.backup_mgr.delete_backup,
                          self.ctxt,
                          backup_id)
        backup = db.backup_get(self.ctxt, backup_id)
        self.assertEqual(backup['status'], 'error')

    def test_delete_backup_with_error(self):
        """Test error handling when an error occurs during backup deletion."""
        vol_id = self._create_volume_db_entry(size=1)
        backup_id = self._create_backup_db_entry(status='deleting',
                                                 display_name='fail_on_delete',
                                                 volume_id=vol_id)
        self.assertRaises(IOError,
                          self.backup_mgr.delete_backup,
                          self.ctxt,
                          backup_id)
        backup = db.backup_get(self.ctxt, backup_id)
        self.assertEqual(backup['status'], 'error')

    def test_delete_backup_with_bad_service(self):
        """Test error handling when attempting a delete of a backup
        with a different service to that used to create the backup.
        """
        vol_id = self._create_volume_db_entry(size=1)
        backup_id = self._create_backup_db_entry(status='deleting',
                                                 volume_id=vol_id)
        service = 'cinder.tests.backup.bad_service'
        db.backup_update(self.ctxt, backup_id, {'service': service})
        self.assertRaises(exception.InvalidBackup,
                          self.backup_mgr.delete_backup,
                          self.ctxt,
                          backup_id)
        backup = db.backup_get(self.ctxt, backup_id)
        self.assertEqual(backup['status'], 'error')

    def test_delete_backup_with_no_service(self):
        """Test error handling when attempting a delete of a backup
        with no service defined for that backup, relates to bug #1162908
        """
        vol_id = self._create_volume_db_entry(size=1)
        backup_id = self._create_backup_db_entry(status='deleting',
                                                 volume_id=vol_id)
        db.backup_update(self.ctxt, backup_id, {'service': None})
        self.backup_mgr.delete_backup(self.ctxt, backup_id)

    def test_delete_backup(self):
        """Test normal backup deletion."""
        vol_id = self._create_volume_db_entry(size=1)
        backup_id = self._create_backup_db_entry(status='deleting',
                                                 volume_id=vol_id)
        self.backup_mgr.delete_backup(self.ctxt, backup_id)
        self.assertRaises(exception.BackupNotFound,
                          db.backup_get,
                          self.ctxt,
                          backup_id)

        ctxt_read_deleted = context.get_admin_context('yes')
        backup = db.backup_get(ctxt_read_deleted, backup_id)
        self.assertEqual(backup.deleted, True)
        self.assertGreaterEqual(timeutils.utcnow(), backup.deleted_at)
        self.assertEqual(backup.status, 'deleted')

    def test_list_backup(self):
        backups = db.backup_get_all_by_project(self.ctxt, 'project1')
        self.assertEqual(len(backups), 0)

        self._create_backup_db_entry()
        b2 = self._create_backup_db_entry(project_id='project1')
        backups = db.backup_get_all_by_project(self.ctxt, 'project1')
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].id, b2)

    def test_backup_get_all_by_project_with_deleted(self):
        """Test deleted backups don't show up in backup_get_all_by_project.
           Unless context.read_deleted is 'yes'.
        """
        backups = db.backup_get_all_by_project(self.ctxt, 'fake')
        self.assertEqual(len(backups), 0)

        backup_id_keep = self._create_backup_db_entry()
        backup_id = self._create_backup_db_entry()
        db.backup_destroy(self.ctxt, backup_id)

        backups = db.backup_get_all_by_project(self.ctxt, 'fake')
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].id, backup_id_keep)

        ctxt_read_deleted = context.get_admin_context('yes')
        backups = db.backup_get_all_by_project(ctxt_read_deleted, 'fake')
        self.assertEqual(len(backups), 2)

    def test_backup_get_all_by_host_with_deleted(self):
        """Test deleted backups don't show up in backup_get_all_by_project.
           Unless context.read_deleted is 'yes'
        """
        backups = db.backup_get_all_by_host(self.ctxt, 'testhost')
        self.assertEqual(len(backups), 0)

        backup_id_keep = self._create_backup_db_entry()
        backup_id = self._create_backup_db_entry()
        db.backup_destroy(self.ctxt, backup_id)

        backups = db.backup_get_all_by_host(self.ctxt, 'testhost')
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].id, backup_id_keep)

        ctxt_read_deleted = context.get_admin_context('yes')
        backups = db.backup_get_all_by_host(ctxt_read_deleted, 'testhost')
        self.assertEqual(len(backups), 2)

    def test_backup_manager_driver_name(self):
        """"Test mapping between backup services and backup drivers."""
        cfg.CONF.set_override('backup_driver', "cinder.backup.services.swift")
        backup_mgr = \
            importutils.import_object(CONF.backup_manager)
        self.assertEqual('cinder.backup.drivers.swift',
                         backup_mgr.driver_name)

    def test_export_record_with_bad_service(self):
        """Test error handling when attempting an export of a backup
        record with a different service to that used to create the backup.
        """
        vol_id = self._create_volume_db_entry(size=1)
        backup_id = self._create_backup_db_entry(status='available',
                                                 volume_id=vol_id)
        service = 'cinder.tests.backup.bad_service'
        db.backup_update(self.ctxt, backup_id, {'service': service})
        self.assertRaises(exception.InvalidBackup,
                          self.backup_mgr.export_record,
                          self.ctxt,
                          backup_id)

    def test_export_record_with_bad_backup_status(self):
        """Test error handling when exporting a backup record with a backup
        with a bad status.
        """
        vol_id = self._create_volume_db_entry(status='available',
                                              size=1)
        backup_id = self._create_backup_db_entry(status='error',
                                                 volume_id=vol_id)
        self.assertRaises(exception.InvalidBackup,
                          self.backup_mgr.export_record,
                          self.ctxt,
                          backup_id)

    def test_export_record(self):
        """Test normal backup record export."""
        vol_size = 1
        vol_id = self._create_volume_db_entry(status='available',
                                              size=vol_size)
        backup_id = self._create_backup_db_entry(status='available',
                                                 volume_id=vol_id)

        export = self.backup_mgr.export_record(self.ctxt, backup_id)
        self.assertEqual(export['backup_service'], CONF.backup_driver)
        self.assertTrue('backup_url' in export)

    def test_import_record_with_verify_not_implemented(self):
        """Test normal backup record import.

        Test the case when import succeeds for the case that the
        driver does not support verify.
        """
        vol_size = 1
        export = self._create_exported_record_entry(vol_size=vol_size)
        imported_record = self._create_export_record_db_entry()
        backup_hosts = []
        self.backup_mgr.import_record(self.ctxt,
                                      imported_record,
                                      export['backup_service'],
                                      export['backup_url'],
                                      backup_hosts)
        backup = db.backup_get(self.ctxt, imported_record)
        self.assertEqual(backup['status'], 'available')
        self.assertEqual(backup['size'], vol_size)

    def test_import_record_with_bad_service(self):
        """Test error handling when attempting an import of a backup
        record with a different service to that used to create the backup.
        """
        export = self._create_exported_record_entry()
        export['backup_service'] = 'cinder.tests.backup.bad_service'
        imported_record = self._create_export_record_db_entry()

        #Test the case where the additional hosts list is empty
        backup_hosts = []
        self.assertRaises(exception.ServiceNotFound,
                          self.backup_mgr.import_record,
                          self.ctxt,
                          imported_record,
                          export['backup_service'],
                          export['backup_url'],
                          backup_hosts)

        #Test that the import backup keeps calling other hosts to find a
        #suitable host for the backup service
        backup_hosts = ['fake1', 'fake2']
        BackupAPI_import = 'cinder.backup.rpcapi.BackupAPI.import_record'
        with mock.patch(BackupAPI_import) as _mock_backup_import:
            self.backup_mgr.import_record(self.ctxt,
                                          imported_record,
                                          export['backup_service'],
                                          export['backup_url'],
                                          backup_hosts)
            self.assertTrue(_mock_backup_import.called)

    def test_import_record_with_invalid_backup(self):
        """Test error handling when attempting an import of a backup
        record where the backup driver returns an exception.
        """
        export = self._create_exported_record_entry()
        backup_driver = self.backup_mgr.service.get_backup_driver(self.ctxt)
        _mock_record_import_class = ('%s.%s.%s' %
                                     (backup_driver.__module__,
                                      backup_driver.__class__.__name__,
                                      'import_record'))
        imported_record = self._create_export_record_db_entry()
        backup_hosts = []
        with mock.patch(_mock_record_import_class) as _mock_record_import:
            _mock_record_import.side_effect = FakeBackupException('fake')
            self.assertRaises(exception.InvalidBackup,
                              self.backup_mgr.import_record,
                              self.ctxt,
                              imported_record,
                              export['backup_service'],
                              export['backup_url'],
                              backup_hosts)
            self.assertTrue(_mock_record_import.called)
        backup = db.backup_get(self.ctxt, imported_record)
        self.assertEqual(backup['status'], 'error')


class BackupTestCaseWithVerify(BaseBackupTest):
    """Test Case for backups."""

    def setUp(self):
        CONF.set_override("backup_driver",
                          "cinder.tests.backup.fake_service_with_verify")
        super(BackupTestCaseWithVerify, self).setUp()

    def test_import_record_with_verify(self):
        """Test normal backup record import.

        Test the case when import succeeds for the case that the
        driver implements verify.
        """
        vol_size = 1
        export = self._create_exported_record_entry(vol_size=vol_size)
        imported_record = self._create_export_record_db_entry()
        backup_hosts = []
        backup_driver = self.backup_mgr.service.get_backup_driver(self.ctxt)
        _mock_backup_verify_class = ('%s.%s.%s' %
                                     (backup_driver.__module__,
                                      backup_driver.__class__.__name__,
                                      'verify'))
        with mock.patch(_mock_backup_verify_class):
            self.backup_mgr.import_record(self.ctxt,
                                          imported_record,
                                          export['backup_service'],
                                          export['backup_url'],
                                          backup_hosts)
        backup = db.backup_get(self.ctxt, imported_record)
        self.assertEqual(backup['status'], 'available')
        self.assertEqual(backup['size'], vol_size)

    def test_import_record_with_verify_invalid_backup(self):
        """Test error handling when attempting an import of a backup
        record where the backup driver returns an exception.
        """
        vol_size = 1
        export = self._create_exported_record_entry(vol_size=vol_size)
        imported_record = self._create_export_record_db_entry()
        backup_hosts = []
        backup_driver = self.backup_mgr.service.get_backup_driver(self.ctxt)
        _mock_backup_verify_class = ('%s.%s.%s' %
                                     (backup_driver.__module__,
                                      backup_driver.__class__.__name__,
                                      'verify'))
        with mock.patch(_mock_backup_verify_class) as _mock_record_verify:
            _mock_record_verify.side_effect = \
                exception.InvalidBackup(reason='fake')

            self.assertRaises(exception.InvalidBackup,
                              self.backup_mgr.import_record,
                              self.ctxt,
                              imported_record,
                              export['backup_service'],
                              export['backup_url'],
                              backup_hosts)
            self.assertTrue(_mock_record_verify.called)
        backup = db.backup_get(self.ctxt, imported_record)
        self.assertEqual(backup['status'], 'error')

    def test_backup_reset_status_from_nonrestoring_to_available(
            self):
        vol_id = self._create_volume_db_entry(status='available',
                                              size=1)
        backup_id = self._create_backup_db_entry(status='error',
                                                 volume_id=vol_id)
        with mock.patch.object(manager.BackupManager,
                               '_map_service_to_driver') as \
                mock_map_service_to_driver:
            mock_map_service_to_driver.return_value = \
                get_backup_driver(self.ctxt)
            self.backup_mgr.reset_status(self.ctxt,
                                         backup_id,
                                         'available')
        backup = db.backup_get(self.ctxt, backup_id)
        self.assertEqual(backup['status'], 'available')

    def test_backup_reset_status_to_available_invalid_backup(self):
        volume = db.volume_create(self.ctxt, {'status': 'available',
                                              'host': 'test',
                                              'provider_location': '',
                                              'size': 1})
        backup = db.backup_create(self.ctxt,
                                  {'status': 'error',
                                   'service':
                                   CONF.backup_driver,
                                   'volume_id': volume['id']})

        backup_driver = self.backup_mgr.service.get_backup_driver(self.ctxt)
        _mock_backup_verify_class = ('%s.%s.%s' %
                                     (backup_driver.__module__,
                                      backup_driver.__class__.__name__,
                                      'verify'))
        with mock.patch(_mock_backup_verify_class) as \
                _mock_record_verify:
            _mock_record_verify.side_effect = \
                exception.BackupVerifyUnsupportedDriver(reason='fake')

            self.assertRaises(exception.BackupVerifyUnsupportedDriver,
                              self.backup_mgr.reset_status,
                              self.ctxt,
                              backup['id'],
                              'available')
            backup = db.backup_get(self.ctxt, backup['id'])
            self.assertEqual(backup['status'], 'error')

    def test_backup_reset_status_from_restoring_to_available(self):
        volume = db.volume_create(self.ctxt,
                                  {'status': 'available',
                                   'host': 'test',
                                   'provider_location': '',
                                   'size': 1})
        backup = db.backup_create(self.ctxt,
                                  {'status': 'restoring',
                                   'service':
                                   CONF.backup_driver,
                                   'volume_id': volume['id']})

        self.backup_mgr.reset_status(self.ctxt,
                                     backup['id'],
                                     'available')
        backup = db.backup_get(self.ctxt, backup['id'])
        self.assertEqual(backup['status'], 'available')

    def test_backup_reset_status_to_error(self):
        volume = db.volume_create(self.ctxt,
                                  {'status': 'available',
                                   'host': 'test',
                                   'provider_location': '',
                                   'size': 1})
        backup = db.backup_create(self.ctxt,
                                  {'status': 'creating',
                                   'service':
                                   CONF.backup_driver,
                                   'volume_id': volume['id']})
        self.backup_mgr.reset_status(self.ctxt,
                                     backup['id'],
                                     'error')
        backup = db.backup_get(self.ctxt, backup['id'])
        self.assertEqual(backup['status'], 'error')
