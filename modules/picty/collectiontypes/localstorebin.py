'''

    picty
    Copyright (C) 2013  Damien Moore

License:

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''

__version__='0.8'


##standard imports
import bisect
from datetime import datetime
import os, shutil
import os.path
import re
import cPickle
import string
import tempfile

import gtk

##picty imports
from picty import pluginmanager
from picty import settings
#todo: this is a workaround in case of no pyinotify (partially for windows)
try:
    from picty.fstools import monitor2 as monitor
except:
    monitor=None
from picty import viewsupport
from picty import baseobjects
from picty import simple_parser as sp
from picty.uitools import dialogs
from picty import imagemanip
from picty import backend
from picty.fstools import io
from picty.uitools import widget_builder as wb
import simpleview


exist_actions=['Skip','Rename','Overwrite Always','Overwrite if Newer']
EXIST_SKIP=0
EXIST_RENAME=1
EXIST_OVERWRITE=2
EXIST_OVERWRITE_NEWER=3


def update_legacy_item(item,image_dir):
    uid=os.path.relpath(item,image_dir)
    print item.uid,'-->',uid
    new_item=baseobjects.Item(uid)
    new_item.mtime=item.mtime ##todo: move this to the meta dictionary
    new_item.thumburi=item.thumburi
    new_item.meta=item.meta ##a copy of new_item.meta will be stored as new_item.meta_backup if meta has been changed but not saved
    return new_item


#class NamingTemplate(string.Template):
#    def __init__(self,template):
#        t=template.replace("<","${").replace(">","}")
#        string.Template.__init__(self,t)

def get_date(item):
    '''
    returns a datetime object containing the date the image was taken or if not available the mtime
    '''
    result=viewsupport.get_ctime(item)
    if result==datetime(1900,1,1):
        return datetime.fromtimestamp(item.mtime)
    else:
        return result

#def get_year(item):
#    '''
#    returns 4 digit year as string
#    '''
#    return '%04i'%get_date(item).year
#
#def get_month(item):
#    '''
#    returns 2 digit month as string
#    '''
#    return '%02i'%get_date(item).month
#
#def get_day(item):
#    '''
#    returns 2 digit day as string
#    '''
#    return '%02i'%get_date(item).day
#
#def get_datetime(item):
#    '''
#    returns a datetime string of the form "YYYYMMDD-HHMMSS"
#    '''
#    d=get_date(item)
#    return '%04i%02i%02i-%02i%02i%02i'%(d.year,d.month,d.day,d.hour,d.minute,d.day)

def get_original_name(item):
    '''
    returns a tuple (path,name) for the transfer destination
    '''
    ##this only applies to locally stored files
    return os.path.splitext(os.path.split(item.uid)[1])[0]


#class VariableExpansion:
#    def __init__(self,item):
#        self.item=item
#        self.variables={
#            'Year':get_year,
#            'Month':get_month,
#            'Day':get_day,
#            'DateTime':get_datetime,
#            'ImageName':get_original_name,
#            }
#    def __getitem__(self,variable):
#        return self.variables[variable](self.item)

#def naming_default(item,dest_dir_base):
#    '''
#    returns a tuple (path,name) for the transfer destination
#    '''
#    return dest_dir_base,os.path.split(item.uid)[1]

def name_item(item,dest_base_dir,naming_scheme):
    #subpath=NamingTemplate(naming_scheme).substitute(VariableExpansion(item))
    subpath=get_date(item).strftime(naming_scheme).replace("<ImageName>",get_original_name(item))
    ext=os.path.splitext(item.uid)[1]
    fullpath=os.path.join(dest_base_dir,subpath+ext)
    return os.path.split(fullpath)

naming_schemes=[
    ("<ImageName>","<ImageName>",False),
    ("<Year>/<Month>/<ImageName>","%Y/%m/<ImageName>",True),
    ("<Y>/<M>/<DateTime>-<ImageName>","%Y/%m/%Y%m%d-%H%M%S-<ImageName>",True),
    ("<Y>/<M>/<Day>/<ImageName>","%Y/%m/%d/<ImageName>",True),
    ("<Y>/<M>/<Day>/<DateTime>-<ImageName>","%Y/%m/%d/%Y%m%d-%H%M%S-<ImageName>",True),
    ]

def altname(pathname):
    dirname,fullname=os.path.split(pathname)
    name,ext=os.path.splitext(fullname)
    i=0
    while os.path.exists(pathname):
        i+=1
        aname=name+'_(%i)'%i
        pathname=os.path.join(dirname,aname+ext)
    return pathname


class LocalTransferOptionsBox(gtk.VBox):
    def __init__(self,collection):
        gtk.VBox.__init__(self)
        self.transfer_frame=gtk.Expander("Advanced Transfer Options")
        self.pack_start(self.transfer_frame)
        self.transfer_box=gtk.VBox()
        self.transfer_frame.add(self.transfer_box)

        self.base_dir_entry=dialogs.PathnameEntry('','',"Choose transfer directory") ##todo:move PathnameEntry to widgetbuilder
        self.base_dir_entry.set_path(collection.image_dirs[0])

        self.widgets=wb.LabeledWidgets([
                ('base_dest_dir','Destination Path:',self.base_dir_entry),
                ('name_scheme','Naming Scheme:',wb.ComboBox([n[0] for n in naming_schemes])),
                ('action_if_exists','Action if Destination Exists:',wb.ComboBox(exist_actions)),
            ])

        self.widgets['action_if_exists'].set_active(0)
        self.widgets['name_scheme'].set_active(3)
        self.transfer_box.pack_start(self.widgets)

    def get_options(self):
        return {
            'base_dest_dir':self.base_dir_entry.get_path(),
            'name_scheme':self.widgets['name_scheme'].get_form_data(),
            'action_if_exists':self.widgets['action_if_exists'].get_form_data(),
            }

    def set_options(self,values):
        self.base_dir_entry.set_path(values['base_dest_dir'])
        self.widgets['name_scheme'].set_form_data(values['name_scheme'])
        self.widgets['action_if_exists'].set_form_data(values['action_if_exists'])



class LocalStorePrefWidget(gtk.VBox):
    def __init__(self,value_dict=None):
        gtk.VBox.__init__(self)
        box,self.name_entry=dialogs.box_add(self,[(gtk.Entry(),True,None)],'Collection Name: ')
        self.path_entry=dialogs.PathnameEntry('','Path to Images: ','Choose a Directory',directory=True)
        self.pack_start(self.path_entry,False)
        self.name_entry.connect("changed",self.name_changed)
        self.path_entry.path_entry.connect("changed",self.path_changed)

        self.a_frame=gtk.Expander("Advanced Options")
        self.a_box=gtk.VBox()
        self.a_frame.add(self.a_box)
        self.recursive_button=gtk.CheckButton("Include sub-directories")
        self.recursive_button.set_active(True)
        self.rescan_check=gtk.CheckButton("Rescan for changes after opening")
        self.rescan_check.set_active(True)
        self.load_meta_check=gtk.CheckButton("Load metadata")
        self.load_meta_check.set_active(True)
        self.monitor_images_check=gtk.CheckButton("Monitor image folders for changes")
        self.monitor_images_check.set_active(True)
        self.use_internal_thumbnails_check=gtk.CheckButton("Use embedded thumbnails if available")
        self.use_internal_thumbnails_check.set_active(False)
        self.store_thumbs_combo=wb.LabeledComboBox("Thumbnail storage",["Gnome Desktop Thumbnail Cache (User's Home)","Hidden Folder in Collection Folder"])
        if settings.is_windows:
            self.store_thumbs_combo.set_sensitive(False)
            self.store_thumbs_combo.set_form_data(1)
        else:
            self.store_thumbs_combo.set_form_data(0)
        self.trash_location_combo=wb.LabeledComboBox("Trash Location",["User's Trash Folder","Hidden .trash Folder in Collection Folder"])
        self.trash_location_combo.set_form_data(0)
        self.store_thumbnails_check=gtk.CheckButton("Store thumbnails in cache")
        self.store_thumbnails_check.set_active(True)
        self.store_thumbnails_check.connect("clicked",self.store_thumbnails_clicked)
        self.sidecar_check=gtk.CheckButton("Use metadata sidecars for unsupported formats")
        self.sidecar_check.set_active(False)
        self.a_box.pack_start(self.recursive_button,False)
        self.a_box.pack_start(self.rescan_check,False)
        self.a_box.pack_start(self.monitor_images_check,False)
        self.a_box.pack_start(self.load_meta_check,False)
        self.a_box.pack_start(self.sidecar_check,False)
        self.a_box.pack_start(self.use_internal_thumbnails_check,False)
        self.a_box.pack_start(self.store_thumbnails_check,False)
        self.a_box.pack_start(self.store_thumbs_combo,False)
        self.a_box.pack_start(self.trash_location_combo,False)
        self.pack_start(self.a_frame,False)
        self.show_all()
        if value_dict:
            self.set_values(value_dict)

    def store_thumbnails_clicked(self,toggle):
        self.store_thumbs_combo.set_sensitive(toggle.get_active())

    def path_changed(self,entry):
        sensitive=len(self.name_entry.get_text().strip())>0 and os.path.exists(self.path_entry.get_path()) ##todo: also check that name is a valid filename

    def name_changed(self,entry):
        sensitive=len(entry.get_text().strip())>0 and os.path.exists(self.path_entry.get_path()) ##todo: also check that name is a valid filename

    def get_values(self):
        return {
                'name': self.name_entry.get_text().replace('/','').strip(),
                'image_dirs': [self.path_entry.get_path()],
                'recursive': self.recursive_button.get_active(),
                'rescan_at_open': self.rescan_check.get_active(),
                'load_meta':self.load_meta_check.get_active(),
                'load_embedded_thumbs':self.use_internal_thumbnails_check.get_active(),
                'load_preview_icons':self.use_internal_thumbnails_check.get_active() and not self.load_meta_check.get_active(),
                'monitor_image_dirs':self.monitor_images_check.get_active(),
                'trash_location':'OPEN-DESKTOP' if self.trash_location_combo.get_form_data()==0 else None,
                'store_thumbnails':self.store_thumbnails_check.get_active(),
                'store_thumbs_with_images':self.store_thumbs_combo.get_form_data(),
                'use_sidecars':self.sidecar_check.get_active(),
                }

    def set_values(self,val_dict):
        self.name_entry.set_text(val_dict['name'])
        if len(val_dict['image_dirs'])>0:
            self.path_entry.set_path(val_dict['image_dirs'][0])
        self.recursive_button.set_active(val_dict['recursive'])
        self.rescan_check.set_active(val_dict['rescan_at_open'])
        self.load_meta_check.set_active(val_dict['load_meta'])
        self.use_internal_thumbnails_check.set_active(val_dict['load_embedded_thumbs'])
        self.monitor_images_check.set_active(val_dict['monitor_image_dirs'])
        self.store_thumbnails_check.set_active(val_dict['store_thumbnails'])
        self.store_thumbs_combo.set_form_data(val_dict['store_thumbs_with_images'])
        self.trash_location_combo.set_form_data(0 if val_dict['trash_location'] is not None else 1)
        self.sidecar_check.set_active(val_dict['use_sidecars'])

class NewLocalStoreWidget(gtk.VBox):
    def __init__(self,main_dialog,value_dict):
        gtk.VBox.__init__(self)
        self.main_dialog=main_dialog
        label=gtk.Label()
        label.set_markup("<b>Local Store Settings</b>")
        self.pack_start(label,False)
        box,self.name_entry=dialogs.box_add(self,[(gtk.Entry(),True,None)],'Collection Name: ')
        self.path_entry=dialogs.PathnameEntry('','Path to Images: ','Choose a Directory',directory=True)
        self.pack_start(self.path_entry,False)
        self.name_entry.connect("changed",self.name_changed)
        self.path_entry.path_entry.connect("changed",self.path_changed)

        self.a_frame=gtk.Expander("Advanced Options")
        self.a_box=gtk.VBox()
        self.a_frame.add(self.a_box)
        self.recursive_button=gtk.CheckButton('Include sub-directories')
        self.recursive_button.set_active(True)
        self.rescan_check=gtk.CheckButton("Rescan for changes after opening")
        self.rescan_check.set_active(True)
        self.load_meta_check=gtk.CheckButton("Load image metadata")
        self.load_meta_check.set_active(True)
        self.use_internal_thumbnails_check=gtk.CheckButton("Use embedded thumbnails if available")
        self.use_internal_thumbnails_check.set_active(False)
        self.store_thumbs_combo=wb.LabeledComboBox("Thumbnail storage",["Gnome Desktop Thumbnail Cache (User's Home)","Hidden Folder in Collection Folder"])
        if settings.is_windows:
            self.store_thumbs_combo.set_sensitive(False)
            self.store_thumbs_combo.set_form_data(1)
        else:
            self.store_thumbs_combo.set_form_data(0)
        self.store_thumbnails_check=gtk.CheckButton("Store thumbnails in cache") #todo: need to implement in backend
        self.store_thumbnails_check.set_active(True)
        self.store_thumbnails_check.connect("clicked",self.store_thumbnails_clicked)
        self.monitor_images_check=gtk.CheckButton("Monitor image folders for changes") #todo: need to implement in backend
        self.monitor_images_check.set_active(True)
        self.trash_location_combo=wb.LabeledComboBox("Trash Location",["User's Trash Folder","Hidden .trash Folder in Collection Folder"])
        self.trash_location_combo.set_form_data(0)
        self.sidecar_check=gtk.CheckButton("Use metadata sidecars for unsupported formats") #todo: need to implement in backend
        self.sidecar_check.set_active(False)


        self.a_box.pack_start(self.recursive_button,False)
        self.a_box.pack_start(self.rescan_check,False)
        self.a_box.pack_start(self.monitor_images_check,False)
        self.a_box.pack_start(self.load_meta_check,False)
        self.a_box.pack_start(self.sidecar_check,False)
        self.a_box.pack_start(self.use_internal_thumbnails_check,False)
        self.a_box.pack_start(self.store_thumbnails_check,False)
        self.a_box.pack_start(self.store_thumbs_combo,False)
        self.a_box.pack_start(self.trash_location_combo,False)

        self.pack_start(self.a_frame,False)
        self.show_all()

#        self.main_dialog.create_button.set_sensitive(False)
        if value_dict:
            self.set_values(value_dict)


    def store_thumbnails_clicked(self,toggle):
        self.store_thumbs_combo.set_sensitive(toggle.get_active())

    def activate(self):
        sensitive=len(self.name_entry.get_text().strip())>0 and os.path.exists(self.path_entry.get_path()) ##todo: also check that name is a valid filename
        self.main_dialog.create_button.set_sensitive(sensitive)

    def path_changed(self,entry):
        sensitive=len(self.name_entry.get_text().strip())>0 and os.path.exists(self.path_entry.get_path()) ##todo: also check that name is a valid filename
        self.main_dialog.create_button.set_sensitive(sensitive)

    def name_changed(self,entry):
        sensitive=len(entry.get_text().strip())>0 and os.path.exists(self.path_entry.get_path()) ##todo: also check that name is a valid filename
        self.main_dialog.create_button.set_sensitive(sensitive)

#    def path_changed(self,entry):

    def get_values(self):
        return {
                'name': self.name_entry.get_text().strip(),
                'image_dirs': [self.path_entry.get_path()],
                'recursive': self.recursive_button.get_active(),
                'rescan_at_open': self.rescan_check.get_active(),
                'load_meta':self.load_meta_check.get_active(),
                'load_embedded_thumbs':self.use_internal_thumbnails_check.get_active(),
                'load_preview_icons':self.use_internal_thumbnails_check.get_active() and not self.load_meta_check.get_active(),
                'monitor_image_dirs':self.monitor_images_check.get_active(),
                'store_thumbs_with_images':self.store_thumbs_combo.get_form_data(),
                'store_thumbnails':self.store_thumbnails_check.get_active(),
                'trash_location':'OPEN-DESKTOP' if self.trash_location_combo.get_form_data()==0 else None,
                'use_sidecars':self.sidecar_check.get_active(),
                }

    def set_values(self,val_dict):
        self.name_entry.set_text(val_dict['name'])
        if len(val_dict['image_dirs'])>0:
            self.path_entry.set_path(val_dict['image_dirs'][0])
        self.recursive_button.set_active(val_dict['recursive'])
        self.rescan_check.set_active(val_dict['rescan_at_open'])
        self.load_meta_check.set_active(val_dict['load_meta'])
        self.use_internal_thumbnails_check.set_active(val_dict['load_embedded_thumbs'])
        self.monitor_images_check.set_active(val_dict['monitor_image_dirs'])
        self.store_thumbnails_check.set_active(val_dict['store_thumbnails'])
        self.store_thumbs_combo.set_form_data(val_dict['store_thumbs_with_images'])
        self.store_thumbnails_check.set_active(val_dict['store_thumbnails'])
        self.trash_location_combo.set_form_data(0 if val_dict['trash_location'] is not None else 1)
        self.sidecar_check.set_active(val_dict['use_sidecars'])


def create_empty_localstore(name,prefs,overwrite_if_exists=False):
    col_dir=os.path.join(settings.collections_dir,name)
    pref_file=os.path.join(os.path.join(settings.collections_dir,name),'prefs')
    data_file=os.path.join(os.path.join(settings.collections_dir,name),'data')
    if not overwrite_if_exists:
        if os.path.exists(col_dir):
            return False
    try:
        if not os.path.exists(col_dir):
            os.makedirs(col_dir)
        f=open(pref_file,'wb')
        cPickle.dump(__version__,f,-1)
        d={} #ensure that preferences associated with older versions are dropped
        for p in Collection.pref_items + ('type',):
            if p in prefs:
                d[p]=prefs[p]
        cPickle.dump(prefs,f,-1)
        f.close()
        f=open(data_file,'wb')
        cPickle.dump(__version__,f,-1)
        cPickle.dump([],f,-1) #empty list of items
        f.close()
    except:
        print 'Error writing empty collection to ',col_dir
        import traceback,sys
        tb_text=traceback.format_exc(sys.exc_info()[2])
        print tb_text
        return False
    return True




class Collection(baseobjects.CollectionBase):
    '''
    Defines a persistent collection of images on the local filesystem
    '''
    type='LOCALSTORE'
    type_descr='Local Store'
    local_filesystem=True
    pref_widget=LocalStorePrefWidget
    add_widget=NewLocalStoreWidget
    metadata_widget=dialogs.MetaDialog
    transfer_widget=LocalTransferOptionsBox
    browser_sort_keys=viewsupport.sort_keys
    persistent=True
    user_creatable=True
    view_class=simpleview.SimpleView
    pref_items=baseobjects.CollectionBase.pref_items+('image_dirs','recursive','verify_after_walk','load_meta','load_embedded_thumbs',
                'load_preview_icons','trash_location','thumbnail_cache_dir','monitor_image_dirs','rescan_at_open','store_thumbnails','store_thumbs_with_images','use_sidecars')
    def __init__(self,prefs): #todo: store base path for the collection
        ##the following attributes are set at run-time by the owner
        baseobjects.CollectionBase.__init__(self)

        self.index = baseobjects.MetadataIndex()

#        ##the collection consists of an array of entries for images, which are cached in the collection file
        self.items=[] #the image/video items

        ##and has the following properties (which are stored in the collection file if it exists)
        self.image_dirs=[]
        self.recursive=True
        self.verify_after_walk=True
        self.load_meta=True #image will be loaded into the collection and view without metadata
        self.load_embedded_thumbs=False #only relevant if load_metadata is true
        self.load_preview_icons=False #only relevant if load_metadata is false
        self.trash_location='OPEN-DESKTOP' #none defaults to <collection dir>/.trash
        self.thumbnail_cache_dir=None #use gnome/freedesktop if none, otherwise specifies a directory (usually a hidden folder below the image collection folder)
        self.monitor_image_dirs=True
        self.store_thumbnails=True
        self.store_thumbs_with_images=False
        self.rescan_at_open=True
        self.use_sidecars=False

        ## the collection optionally has a filesystem monitor and views (i.e. subsets) of the collection of images
        self.monitor=None
        self.monitor_master_callback=None
        self.browser=None
        self.online=False

        if prefs:
            self.set_prefs(prefs)

        self.id=self.name

    ''' ************************************************************************
                            PREFERENCES, OPENING AND CLOSING
        ************************************************************************'''
    def set_prefs(self,prefs):
        baseobjects.CollectionBase.set_prefs(self,prefs)
        if settings.is_windows or self.store_thumbs_with_images:
            self.thumbnail_cache_dir=os.path.join(self.image_dirs[0],'.thumbnails')
        else:
            self.thumbnail_cache_dir=None
    def connect(self):
        if not self.is_open:
            return False
        if self.online:
            return False
        self.online=True
        self.start_monitor()
        return True
    def disconnect(self):
        if not self.is_open:
            return False
        self.end_monitor()
        self.online=False
        return True
    def delete_store(self):
        col_dir=os.path.join(settings.collections_dir,self.name)
        try:
            if os.path.isdir(col_dir):
                for root, dirs, files in os.walk(col_dir, topdown=False):
                    for name in files:
                        os.remove(os.path.join(root, name))
                    for name in dirs:
                        os.rmdir(os.path.join(root, name))
                os.rmdir(col_dir)
            elif os.path.isfile(col_dir):
                io.remove_file(col_dir)
            return True
        except IOError:
            print 'Error removing collection data files in',col_dir
            import sys,traceback
            tb_text=traceback.format_exc(sys.exc_info()[2])
            print tb_text
            return False

    def create_store(self):
        return create_empty_localstore(self.name,self.get_prefs())

    def open(self,thread_manager,browser=None):
        self.start_monitor(thread_manager.directory_change_notify) ##todo: THIS SHOULDN'T HAPPEN UNTIL AFTER WE SUCCESSFULLY OPEN
        j=backend.LoadCollectionJob(thread_manager,self,browser)
        thread_manager.queue_job_instance(j)

    def _open(self):
        '''
        load the collection from a binary pickle file
        '''
        col_dir=os.path.join(settings.collections_dir,self.name)
        if self.is_open:
            return True
        try:
            if os.path.isfile(col_dir):
                return self.legacy_open(col_dir)
            f=open(self.data_file(),'rb')
            version=cPickle.load(f)
            print 'Loaded collection %s (version %s)'%(self.name,version,)
            if version>='0.5':
                try:
                    self.items=cPickle.load(f)
                except:
                    pass
            if version>='0.8':
                try:
                    self.views[0].load(f)
                except:
                    pass
            if version<'0.7':
                print 'Updating legacy collection'
                self.items=[update_legacy_item(i,self.image_dirs[0]) for i in self.items]
                print 'Update complete'
            self.numselected=0
            for i in self.items: #todo: this could be cached as well
                self.index.add(i)
            return True
        except:
            import traceback,sys
            tb_text=traceback.format_exc(sys.exc_info()[2])
            print "Error Loading Collection",self.name
            print tb_text
            self.empty()
            return False

    def close(self):
        '''
        save the collection to a binary pickle file using the filename attribute of the collection
        '''
        if not self.is_open:
            return True
        if not self.persistent:
            return True
        try:
            col_dir=os.path.join(settings.collections_dir,self.name)
            if os.path.isfile(col_dir):
                os.remove(col_dir)
            if not os.path.exists(col_dir):
                os.makedirs(col_dir)
            #self.save_prefs()
            f=open(self.data_file(),'wb')
            cPickle.dump(__version__,f,-1)
            cPickle.dump(self.items,f,-1)
            self.get_active_view().save(f)
            f.close()
            self.empty()
        except:
            import traceback,sys
            tb_text=traceback.format_exc(sys.exc_info()[2])
            print "Error Writing Collection",self.name
            print tb_text
            return False
        return True

    def rescan(self,thead_manager):
        sj=backend.WalkDirectoryJob(thead_manager,self,self.browser)
        thead_manager.queue_job_instance(sj)

    ''' ************************************************************************
                            MONITORING THE COLLECTION
        ************************************************************************'''

    def start_monitor(self,callback=None):
        if monitor == None:
            return
        if self.monitor_image_dirs:
            if callback!=None:
                self.monitor_master_callback=callback
            print 'STARTING MONITOR'
            self.monitor=monitor.Monitor(self.image_dirs,self.recursive,self.monitor_callback)
            print 'STARTED MONITOR'

    def end_monitor(self):
        if self.monitor!=None and self.monitor_image_dirs:
            self.monitor.stop()
            self.monitor=None

    def monitor_callback(self,path,action,is_dir):
        self.monitor_master_callback(self,path,action,is_dir)



    ''' ************************************************************************
                            MANAGING THE LIST OF COLLECTION ITEMS
        ************************************************************************'''

    def add(self,item,add_to_view=True):
        '''
        add an item to the collection and notify plugin
        '''
        try:
            print 'Adding',item,'to collection',self
            ind=bisect.bisect_left(self.items,item)
            if len(self.items)>ind>=0 and self.items[ind]==item:
                raise LookupError
            self.items.insert(ind,item)
            self.numselected+=item.selected
            pluginmanager.mgr.callback_collection('t_collection_item_added',self,item)
            if add_to_view:
                for v in self.views:
                    v.add_item(item)
            if self.index:
                self.index.add(item)
            return True
        except LookupError:
            print 'WARNING: tried to add',item,ind,'to collection',self.id,'but an item with this id was already present'
            import traceback,sys
            tb_text=traceback.format_exc(sys.exc_info()[2])
            print tb_text
            return False

    def delete(self,item,delete_from_view=True):
        '''
        delete an item from the collection, returning the item to the caller if present
        notifies plugins if the item is remmoved
        '''
        i=self.find(item)
        if i>=0:
            print 'Removing',item,'from collection',self
            item=self.items[i]
            self.numselected-=item.selected
            self.items.pop(i)
            pluginmanager.mgr.callback_collection('t_collection_item_removed',self,item)
            for v in self.views:
                v.del_item(item)
            if self.index:
                self.index.remove(item)
            return item
        return None

    def find(self,item):
        '''
        find an item in the collection and return its index
        '''
        i=bisect.bisect_left(self,item)
        if i>=len(self.items) or i<0:
            return -1
        if self.items[i]==item:
            return i
        return -1

    def get_mtime(self,item):
        return io.get_mtime(self.get_path(item))

    def get_path(self,item):
        '''
        returns the full path associated with the item
        beware that this uses the hack that the item is derived from str
        and the content of the string is the uid
        '''
        return os.path.join(self.image_dirs[0],item)

    def item_exists(self,item):
        return os.path.exists(self.get_path(item))

    def get_relpath(self,path):
        return os.path.relpath(path,self.image_dirs[0])

    def __call__(self,ind):
        return self.items[ind]

    def __getitem__(self,ind):
        return self.items[ind]

    def get_all_items(self):
        return self.items[:]

    def empty(self,empty_views=True):
        del self.items[:]
        self.numselected=0
        if empty_views:
            for v in self.views:
                v.empty()

    def __len__(self):
        return len(self.items)

    ''' ************************************************************************
                            MANIPULATING INDIVIDUAL ITEMS
        ************************************************************************'''
    def copy_item(self,src_collection,src_item,prefs):
        'copy an item from another collection source'
        try:
            print 'copying item',src_item,prefs
            name=os.path.split(src_item.uid)[1]
            dest_dir=prefs['base_dest_dir']
            src_filename=None
            if src_collection.local_filesystem:
                src_filename=src_collection.get_path(src_item)
            temp_filename=''
            temp_dir=''
            name_scheme=naming_schemes[prefs['name_scheme']]
            dest_name_template=name_scheme[1]
            dest_needs_meta=name_scheme[2]

            if dest_needs_meta and src_item.meta==None:
                temp_dir=tempfile.mkdtemp('','.image-',dest_dir)
                temp_filename=os.path.join(temp_dir,name)
                try:
                    if src_collection.local_filesystem:
                        io.copy_file(src_filename,temp_filename) ##todo: this may be a desirable alternative for local images
                        if src_collection.use_sidecars and 'sidecar' in src_item.__dict__ and os.path.exists(src_collection.get_path(src_item.sidecar)):
                            temp_sidecar=os.path.join(temp_dir,os.path.split(src_item.sidecar)[1])
                            io.copy_file(src_collection.get_path(src_item.sidecar),temp_sidecar)
                    else:
                        open(temp_filename,'wb').write(src_collection.get_file_stream(src_item).read())
                except IOError:
                    print 'Error copying file',src_item
                    import traceback,sys
                    tb_text=traceback.format_exc(sys.exc_info()[2])
                    print tb_text
                    ##todo: log an error
                    ##todo: maybe better to re-raise the exception here
                    return False
                src_filename=temp_filename
                try:
                    imagemanip.load_metadata(src_item,self,src_filename)
                except:
                    src_item.meta={}
            dest_path,dest_name=name_item(src_item,dest_dir,dest_name_template)
            if not src_collection.local_filesystem:
                local_name=src_collection.get_file_name(src_item)
                if local_name:
                    dest_name=local_name
            if not os.path.exists(dest_path):
                os.makedirs(dest_path)
            dest_filename=os.path.join(dest_path,dest_name)
            print 'copying to dest filename',dest_filename
            if os.path.exists(dest_filename):
                if prefs['action_if_exists']==EXIST_SKIP:
                    print 'SKIPPING',src_item
                    ##TODO: LOGGING TO IMPORT LOG
                    return False
                if prefs['action_if_exists']==EXIST_RENAME:
                    dest_filename=altname(dest_filename)

            try:
                if prefs['move_files'] or temp_filename:
                    io.move_file(src_filename,dest_filename,overwrite=prefs['action_if_exists']==EXIST_OVERWRITE)
                else:
                    if src_collection.local_filesystem:
                        io.copy_file(src_filename,dest_filename,overwrite=prefs['action_if_exists']==EXIST_OVERWRITE)
                    else:
                        open(dest_filename,'wb').write(src_collection.get_file_stream(src_item).read())
            except IOError:
                ##todo: log an error
                ##todo: maybe better to re-raise the exception here
                print 'Error copying image',src_item
                import traceback,sys
                tb_text=traceback.format_exc(sys.exc_info()[2])
                print tb_text
                return False

            try:
                if prefs['move_files'] and not temp_filename:
                    src_collection.delete(src_item)
                if temp_filename and temp_filename!=src_filename:
                    io.remove_file(temp_filename)
                if temp_dir:
                    shutil.rmtree(temp_dir)
            except IOError:
                ##todo: log an error
                ##todo: maybe better to re-raise the exception here
                print 'Error cleaning up after copying image',src_item
                import traceback,sys
                tb_text=traceback.format_exc(sys.exc_info()[2])
                print tb_text

            item=baseobjects.Item(self.get_relpath(dest_filename))
            item.mtime=io.get_mtime(dest_filename)
            item.selected=src_item.selected
            #copy metadata from the original
            #TODO: drop metadata not supported by local store?
            if src_item.meta is not None:
                item.init_meta(src_item.meta.copy(),self)
            if src_collection.local_filesystem:
                #for local filesystems lets copy the backup metadata to ensure that the use
                #will see there is unsaved data. could also just write the metadata first, but
                #this gives the user a bit more flexibility.
                if 'meta_backup' in dir(src_item):
                    item.meta_backup=src_item.meta_backup
            else:
                #the item.meta might contain data that isn't in the original, so lets write
                #it to the image (since a localstore image should always represent what is in
                #the image) and then reload it to get anything else that wasn't in item.meta
                self.write_metadata(item) #potential risk of data loss by writing data here
                self.load_metadata(item,notify_plugins=False) ##TODO: Shoudln't we notify plugins?
            self.make_thumbnail(item)
            self.add(item) ##todo: should we lock the image browser rendering updates for this call??
            return True
        except:
            print 'Error copying src item'
            import traceback,sys
            tb_text=traceback.format_exc(sys.exc_info()[2])
            print tb_text
            return False
    def delete_item(self,item):
        'remove the item from the collection and the underlying filestore'
        try:
            if self.trash_location is None:
                trashdir=os.path.join(self.image_dirs[0],'.trash')
                empty,imdir,relpath=self.get_path(item).partition(self.image_dirs[0])
                relpath=relpath.strip('/')
                if relpath:
                    dest=os.path.join(trashdir,relpath)
                    if os.path.exists(dest):
                        os.remove(dest)
                    os.renames(self.get_path(item),dest)
            elif self.trash_location=='OPEN-DESKTOP':
                io.trash_file(self.get_path(item))
            self.delete_thumbnail(item)
            self.delete(item) ##todo: lock the image browser??
            return True
        except:
            print 'Error deleting',item.uid
            import traceback,sys
            tb_text=traceback.format_exc(sys.exc_info()[2])
            print tb_text
            return False
    def load_thumbnail(self,item,fast_only=True):
        'load the thumbnail from the local cache'
        if self.load_preview_icons:
            if imagemanip.load_thumb_from_preview_icon(item,self):
                return True
        if fast_only and not item.thumburi and self.load_embedded_thumbs:
            if imagemanip.load_embedded_thumb(item,self):
                return True
        return imagemanip.load_thumb(item,self,self.thumbnail_cache_dir)
    def has_thumbnail(self,item):
        return imagemanip.has_thumb(item,self,self.thumbnail_cache_dir)
    def make_thumbnail(self,item,interrupt_fn=None,force=False):
        'create a cached thumbnail of the image'
        if not force and (self.load_embedded_thumbs or self.load_preview_icons):
            return False
        imagemanip.make_thumb(item,self,interrupt_fn,force,self.thumbnail_cache_dir,write_to_cache = self.store_thumbnails)
## TODO: Why was the update_thumb_date call here??? Maybe a FAT issue?
##        imagemanip.update_thumb_date(item,cache=self.thumbnail_cache_dir)
        return
    def delete_thumbnail(self,item):
        'clear out the thumbnail and delete the file from the users gnome desktop cache'
        imagemanip.delete_thumb(item)
    def rotate_thumbnail(self,item,right=True,interrupt_fn=None):
        '''
        rotates thumbnail of item 90 degrees right (clockwise) or left (anti-clockwise)
        right - rotate right if True else left
        interrupt_fn - callback that returns False if job should be interrupted
        '''
        if item.thumb==False:
            return False
        thumb_pb=imagemanip.rotate_thumb(item,right,interrupt_fn)
        if not thumb_pb:
            return  False
        item.thumb=thumb_pb
        imagemanip.cache_thumb_in_memory(item)
        uri = io.get_uri(self.get_path(item))
        if self.thumbnail_cache_dir==None:
            imagemanip.thumb_factory.save_thumbnail(thumb_pb,uri,int(item.mtime))
            item.thumburi=imagemanip.thumb_factory.lookup(uri,int(item.mtime))
        else:
            cache=self.thumbnail_cache_dir
            if not os.path.exists(cache):
                os.makedirs(cache)
            item.thumb.save(item.thumburi,"png")
        return True

    def item_metadata_update(self,item,old_metadata):
        'collection will receive this call when item metadata has been changed'
        if self.index:
            self.index.update(item,old_metadata)
    def load_metadata(self,item,missing_only=False,notify_plugins=True):
        'retrieve metadata for an item from the source'
        if self.load_embedded_thumbs:
            result=imagemanip.load_metadata(item,collection=self,filename=self.get_path(item),
                get_thumbnail=True,missing_only=missing_only,check_for_sidecar=self.use_sidecars,
                notify_plugins=notify_plugins)
        else:
            result=imagemanip.load_metadata(item,collection=self,filename=self.get_path(item),
                get_thumbnail=False,missing_only=missing_only,check_for_sidecar=self.use_sidecars,
                notify_plugins=notify_plugins)
        if self.load_embedded_thumbs and not item.thumb:
            item.thumb=False
        return result
    def write_metadata(self,item):
        'write metadata for an item to the source'
        return imagemanip.save_metadata(item,self,cache=self.thumbnail_cache_dir,sidecar_on_failure=self.use_sidecars)
    def load_image(self,item,interrupt_fn=None,size_bound=None,apply_transforms=True):
        'load the fullsize image, up to maximum size given by the (width, height) tuple in size_bound'
        draft_mode=False
        return imagemanip.load_image(item,self,interrupt_fn,draft_mode,apply_transforms=apply_transforms)
    def get_file_stream(self,item):
        'return a stream read the entire photo file from the source (as binary stream)'
        return open(self.get_path(item),'rb')
    def write_file_data(self,dest_item,src_stream):
        'write the entire photo file (as a stream) to the dest_item (as binary stream)'
        try:
            f=open(self.get_path(dest_item),'wb')
            f.write(src_stream.read())
            f.close()
            return True
        except:
            print 'Error writing file data',dest_item
    def get_browser_text(self,item):
        header=''
        if settings.overlay_show_title:
            try:
                header=item.meta['Title']
            except:
                header=os.path.split(item.uid)[1]
        details=''
        if settings.overlay_show_path:
            details+=os.path.split(self.get_path(item))[0]
        if settings.overlay_show_tags:
            val=viewsupport.get_keyword(item)
            if val:
                if details and not details.endswith('\n'):
                    details+='\n'
                val=str(val)
                if len(val)<90:
                    details+='Tags: '+val
                else:
                    details+=val[:88]+'...'
        if settings.overlay_show_date:
            val=viewsupport.get_ctime(item)
            if val>datetime(1900,1,1):
                if details and not details.endswith('\n'):
                    details+='\n'
                details+='Date: '+str(val)
    #    else:
    #        details+='Mod: '+str(get_mtime(item))
        if settings.overlay_show_exposure:
            val=viewsupport.get_focal(item)
            exposure=u''
            if val:
                exposure+='%imm '%(int(val),)
            val=viewsupport.get_aperture(item)
            if val:
                exposure+='f/%3.1f'%(val,)
            val=viewsupport.get_speed_str(item)
            if val:
                exposure+=' %ss'%(val,)
            val=viewsupport.get_iso_str(item)
            if val:
                exposure+=' iso%s'%(val,)
            if exposure:
                if details and not details.endswith('\n'):
                    details+='\n'
                details+=exposure
        return (header,details)

    def get_viewer_text(self,item,size=None,zoom=None):
        ##HEADER TEXT
        header=''
        #show title
        path,filename=os.path.split(self.get_path(item))
        try:
            header=item.meta['Title']
            title=True
        except:
            header+=filename
            title=False

        ##DETAIL TEXT
        details=''
        #show filename and path to image
        if title:
            details+=filename+'\n'
        details+=path
        #show tags
        val=viewsupport.get_keyword(item)
        if val:
            if details and not details.endswith('\n'):
                details+='\n'
            val=str(val)
            if len(val)<90:
                details+='Tags: '+val
            else:
                details+=val[:88]+'...'
        #date information
        if details and not details.endswith('\n'):
            details+='\n'
        val=viewsupport.get_ctime(item)
        if val>datetime(1900,1,1):
            details+='Date: '+str(val)+'\n'
    ###    details+='Date Modified: '+str(get_mtime(item))
        if item.meta!=None and 'Model' in item.meta:
            details+='Model: '+str(item.meta['Model'])+'\n'
        #Exposure details
        val=viewsupport.get_focal(item)
        exposure=u''
        if val:
            exposure+='%imm '%(int(val),)
        val=viewsupport.get_aperture(item)
        if val:
            exposure+='f/%3.1f'%(val,)
        val=viewsupport.get_speed_str(item)
        if val:
            exposure+=' %ss'%(val,)
        val=viewsupport.get_iso_str(item)
        if val:
            exposure+=' iso%s'%(val,)
        if exposure:
            if details and not details.endswith('\n'):
                details+='\n'
            details+='Exposure: '+exposure
        #IMAGE SIZE AND ZOOM LEVEL
        if size:
            if details and not details.endswith('\n'):
                details+='\n'
            details+='Image Dimensions: %i x %i'%size
        if zoom:
            if details and not details.endswith('\n'):
                details+='\n'
            if zoom!='fit':
                details+='Zoom: %3.2f%%'%(zoom*100,)
            else:
                details+='Zoom: Fit'

        return (header,details)


baseobjects.register_collection('LOCALSTORE',Collection)
