#!/usr/bin/python

'''

    phraymd Import plugin
    Copyright (C) 2009  Damien Moore

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

'''
photo importer
in gnome:
    all devices are mounted into the filesystem
    an import source is just a directory
    the import destination is somewhere in the collection directory
    an import operation is a copy or move from source to destination
    the most basic import just copies all supported files across to a destination in the collection directory
    option to obey directory structure of source
    option to remove images from the source
    option to copy images into a date based folder structure using exif date taken
    nameclash option: don't copy or use an alternate name
    ability to set metadata
    keep an import log -- full src and dest path of every file copied or moved + names of files ignored
    option to select images to import or import all -- use the internal browser to handle this
    a custom filter in the browser for viewing the most recent import
    dbus interface to handle user plugging in camera/device containing images (should open sidebar and make src dir the volume)
    if user browses the volume then the full scan/verify/create thumb steps are going to happen. once the users chooses which images to import we don't want to have to redo that step -- should just be able to copy the item objects across (and rename them appropriately) and reuse the thumbnails.

maybe need a gphoto alternative for non-gnome desktops
'''

import os
import os.path
import threading
import tempfile
import string
import datetime
import re

import gtk
import gobject

from phraymd import metadatadialogs
from phraymd import settings
from phraymd import pluginbase
from phraymd import pluginmanager
from phraymd import imageinfo
from phraymd import imagemanip
from phraymd import io
from phraymd import collections
from phraymd import views
from phraymd import metadata
from phraymd import backend
from phraymd import collectionmanager

exist_actions=['Skip','Rename','Overwrite Always','Overwrite if Newer']
EXIST_SKIP=0
EXIST_RENAME=1
EXIST_OVERWRITE=2
EXIST_OVERWRITE_NEWER=3


class NamingTemplate(string.Template):
    def __init__(self,template):
        print 'naming template',template
        t=template.replace("<","${").replace(">","}")
        print 'subbed naming template',t
        string.Template.__init__(self,t)

def get_date(item):
    '''
    returns a datetime object containing the date the image was taken or if not available the mtime
    '''
    result=views.get_ctime(item)
    if result==datetime.datetime(1900,1,1):
        return datetime.datetime.fromtimestamp(item.mtime)
    else:
        return result

def get_year(item):
    '''
    returns 4 digit year as string
    '''
    return '%04i'%get_date(item).year

def get_month(item):
    '''
    returns 2 digit month as string
    '''
    return '%02i'%get_date(item).month

def get_day(item):
    '''
    returns 2 digit day as string
    '''
    return '%02i'%get_date(item).day

def get_datetime(item):
    '''
    returns a datetime string of the form "YYYYMMDD-HHMMSS"
    '''
    d=get_date(item)
    return '%04i%02i%02i-%02i%02i%02i'%(d.year,d.month,d.day,d.hour,d.minute,d.day)

def get_original_name(item):
    '''
    returns a tuple (path,name) for the import destination
    '''
    return os.path.splitext(os.path.split(item.filename)[1])[0]


class VariableExpansion:
    def __init__(self,item):
        self.item=item
        self.variables={
            'Year':get_year,
            'Month':get_month,
            'Day':get_day,
            'DateTime':get_datetime,
            'ImageName':get_original_name,
            }
    def __getitem__(self,variable):
        return self.variables[variable](self.item)

#def naming_default(item,dest_dir_base):
#    '''
#    returns a tuple (path,name) for the import destination
#    '''
#    return dest_dir_base,os.path.split(item.filename)[1]

def name_item(item,dest_base_dir,naming_scheme):
    subpath=NamingTemplate(naming_scheme).substitute(VariableExpansion(item))
    ext=os.path.splitext(item.filename)[1]
    fullpath=os.path.join(dest_base_dir,subpath+ext)
    return os.path.split(fullpath)

naming_schemes=[
    ("<ImageName>","<ImageName>",False),
    ("<Year>/<Month>/<ImageName>","<Year>/<Month>/<ImageName>",True),
    ("<Year>/<Month>/<DateTime>-<ImageName>","<Year>/<Month>/<DateTime>-<ImageName>",True),
    ("<Year>/<Month>/<Day>/<ImageName>","<Year>/<Month>/<Day>/<ImageName>",True),
    ("<Year>/<Month>/<Day>/<DateTime>-<ImageName>","<Year>/<Month>/<Day>/<DateTime>-<ImageName>",True),
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

class ImporterImportJob(backend.WorkerJob):
    def __init__(self,plugin,collection_src,collection_dest,params):
        backend.WorkerJob.__init__(self,'IMPORT',plugin.mainframe.tm.get_default_job_tuple())
        self.plugin=plugin
        self.collection_src=None
        self.collection_dest=None
        for p in params:
            self.__dict__[p]=params[p]
##        self.plugin.mainframe.tm.queue_job_instance(self)

    def cancel_import(self):
        self.cancel=True

    def unsetevent(self):
        backend.WorkerJob.unsetevent(self)
        self.cancel=False
        self.countpos=0
        self.items=None
        self.move=False
        self.action_if_exists=EXIST_SKIP #EXIST_SKIP|EXIST_RENAME|EXIST_OVERWRITE
        self.dest_name_needs_meta=False
        self.dest_name_template=''
        self.base_dest_dir=None
        self.cancel=False

    def __call__(self):
        jobs=worker.jobs
        pluginmanager.mgr.suspend_collection_events(self.collection_dest)
        i=self.countpos
        collection=self.collection_dest
        if self.items==None:
            self.items=self.collection_src.get_active_view().get_selected_items()
            self.count=len(self.items)
        if not self.base_dest_dir:
            self.base_dest_dir=collection.image_dirs[0]
        print 'importing',len(self.items),'items'
        if not os.path.exists(self.base_dest_dir):
            os.makedirs(self.base_dest_dir)
        while len(self.items)>0 and jobs.ishighestpriority(self) and not self.cancel:
            item=self.items.pop()
            self.dest_dir=self.base_dest_dir
            src_filename=item.filename
            temp_filename=''
            temp_dir=''
            if self.dest_name_needs_meta and not item.meta:
                temp_dir=tempfile.mkdtemp('','.image-',self.base_dest_dir)
                temp_filename=os.path.join(temp_dir,os.path.split(item.filename)[1])
                try:
                    io.copy_file(item.filename,temp_filename)
                except IOError:
                    ##todo: log an error
                    continue
                src_filename=temp_filename
                imagemanip.load_metadata(item,self.collection,src_filename)
            dest_path,dest_name=name_item(item,self.base_dest_dir,self.dest_name_template)
            if not os.path.exists(dest_path):
                os.makedirs(dest_path)
            dest_filename=os.path.join(dest_path,dest_name)
            if os.path.exists(dest_filename):
                if self.action_if_exists==EXIST_SKIP:
                    ##TODO: LOGGING TO IMPORT LOG
                    continue
                if self.action_if_exists==EXIST_RENAME:
                    dest_filename=altname(dest_filename)
            try:
                ##todo: set mtime to the mtime of the original after copy/move?
                if self.move or temp_filename:
                    io.move_file(src_filename,dest_filename,overwrite=self.action_if_exists==EXIST_OVERWRITE)
                else:
                    io.copy_file(src_filename,dest_filename,overwrite=self.action_if_exists==EXIST_OVERWRITE)
                if temp_filename:
                    io.remove_file(temp_filename)
                    if self.move and item.filename!=src_filename:
                        io.remove_file(item.filename)
                if temp_dir:
                    os.rmdir(temp_dir)
            except IOError:
                ##todo: log an error
                continue
            item[0]=dest_filename
            item.filename=dest_filename
            item.mtime=io.get_mtime(item.filename)
            if collection.load_metadata and not item.meta:
                imagemanip.load_metadata(item,collection)
            if self.collection_src.load_embedded_thumbs or self.collection_src.load_preview_icons:
                if not collection.load_embedded_thumbs and not collection.load_preview_icons:
                    print 'recreating thumbnail on import for',item.filename
                    imagemanip.make_thumb(item)
            imagemanip.update_thumb_date(item)
            browser.lock.acquire()
            print 'importing item',item.filename,'to',collection.filename
            collection.add(item)
            browser.lock.release()
            ##todo: log success
            i+=1
            gobject.idle_add(browser.update_status,1.0*i/self.count,'Importing media - %i of %i'%(i,self.count))
            gobject.idle_add(browser.refresh_view)
        self.countpos=i
        if not self.state:
            self.idle_add(self.plugin.import_cancelled)
            worker.monitor.start(collection.image_dirs[0])
        if len(self.items)==0 or self.cancel:
            gobject.idle_add(browser.update_status,2,'Import Complete')
            gobject.idle_add(self.plugin.import_completed)
            pluginmanager.mgr.resume_collection_events(self.collection)
            worker.monitor.start(collection.image_dirs[0])
            self.collection_src=None
            self.collection_dest=None
            #jobs['VERIFYIMAGES'].setevent()
            self.unsetevent()


class ImportPlugin(pluginbase.Plugin):
    name='Import'
    display_name='Import Sidebar'
    api_version='0.1.0'
    version='0.1.0'
    def __init__(self):
        pass

    def plugin_init(self,mainframe,app_init):
        self.mainframe=mainframe

        def box_add(box,widget_data,label_text):
            hbox=gtk.HBox()
            if label_text:
                label=gtk.Label(label_text)
                hbox.pack_start(label,False)
            for widget in widget_data:
                hbox.pack_start(widget[0],True)
                if widget[1]:
                    widget[0].connect(widget[1],widget[2])
            box.pack_start(hbox,False)
            return tuple([hbox]+[widget[0] for widget in widget_data])

        self.vbox=gtk.VBox()
#        self.import_source_entry=metadatadialogs.PathnameEntry('',
#            "From Path","Choose Import Source Directory")
#        self.vbox.pack_start(self.import_source_entry,False)
        self.src_combo=collectionmanager.CollectionCombo(mainframe.coll_set.add_model('SELECTOR'))
        self.dest_combo=collectionmanager.CollectionCombo(mainframe.coll_set.add_model('SELECTOR'))
        box_add(self.vbox,[(self.src_combo,"collection-changed",self.src_changed)],"Source")
        box_add(self.vbox,[(self.dest_combo,"collection-changed",self.dest_changed)],"Destination")
#        self.vm=io.VolumeMonitor()
#        self.import_source_combo=metadatadialogs.PathnameCombo("","Import from","Select directory to import from",volume_monitor=self.vm,directory=True)
#        self.vbox.pack_start(self.import_source_combo,False)
        ##SETTINGS

#        ##BROWSING OPTIONS -- todo: use this as a collection browsing dialog
#        self.browse_frame=gtk.Expander("Browsing Options")
#        self.browse_box=gtk.VBox()
#        self.browse_frame.add(self.browse_box)
#        self.browse_read_meta_check=gtk.CheckButton("Load Metadata")
#        self.browse_use_internal_thumbnails_check=gtk.CheckButton("Use Internal Thumbnails")
#        self.browse_use_internal_thumbnails_check.set_active(True)
#        self.browse_store_thumbnails_check=gtk.CheckButton("Store Thumbnails in Cache")
#        self.browse_box.pack_start(self.browse_read_meta_check,False)
#        self.browse_box.pack_start(self.browse_use_internal_thumbnails_check,False)
#        #self.browse_box.pack_start(self.browse_store_thumbnails_check,False) ##todo: switch this back on and implement in backend/imagemanip
#        self.vbox.pack_start(self.browse_frame,False)

        ##IMPORT OPTIONS
        self.import_frame=gtk.Expander("Import Options")
        self.import_box=gtk.VBox()
        self.import_frame.add(self.import_box)
        self.base_dir_entry=metadatadialogs.PathnameEntry('', ##self.mainframe.tm.collection.image_dirs[0]
            "To Path","Choose import directory")
        self.import_box.pack_start(self.base_dir_entry,False)

        self.name_scheme_model=gtk.ListStore(str,gobject.TYPE_PYOBJECT,gobject.TYPE_BOOLEAN) ##display, callback, uses metadata
        for n in naming_schemes:
            self.name_scheme_model.append(n)
        self.name_scheme_combo=gtk.ComboBox(self.name_scheme_model)
        box,self.name_scheme_combo=box_add(self.import_box,[(self.name_scheme_combo,None)],"Naming Scheme")
        cell = gtk.CellRendererText()
        self.name_scheme_combo.pack_start(cell, True)
        self.name_scheme_combo.add_attribute(cell, 'text', 0)
        self.name_scheme_combo.set_active(0)

        box,self.exists_combo=box_add(self.import_box,[(gtk.combo_box_new_text(),None)],"Action if Destination Exists")
        for x in exist_actions:
            self.exists_combo.append_text(x)
        self.exists_combo.set_active(0)

        self.copy_radio=gtk.RadioButton(None,"_Copy",True)
        self.move_radio=gtk.RadioButton(self.copy_radio,"_Move",True)
        box_add(self.import_box,[(self.copy_radio,None),(self.move_radio,None)],"Import Operation")

        self.vbox.pack_start(self.import_frame,False)

        self.import_action_box,self.start_import_all_button,self.start_import_button=box_add(self.vbox,
            [(gtk.Button("Import All"),"clicked",self.start_import,True),
            (gtk.Button("Import Selected"),"clicked",self.start_import,False)],
            "")

#        self.mode_box,button1,button2=box_add(self.vbox,
#            [(gtk.Button("Import Now"),"clicked",self.import_now),
#            (gtk.Button("Import Now"),"clicked",self.import_now)],
#            "")
#        button1.set_sensitive(False)
        #button2.set_sensitive(False)

        self.scrolled_window=gtk.ScrolledWindow() ##todo: use a custom Notebook to embed all pages in a scrolled window automatically
        self.scrolled_window.set_policy(gtk.POLICY_AUTOMATIC,gtk.POLICY_AUTOMATIC)
        self.scrolled_window.add_with_viewport(self.vbox)
        self.scrolled_window.show_all()
#        self.import_frame.hide()
#        self.import_action_box.hide()

        self.coll_src=None
        self.coll_dest=None

        self.mainframe.sidebar.append_page(self.scrolled_window,gtk.Label("Import"))

    def plugin_shutdown(self,app_shutdown):
        if not app_shutdown:
            self.scrolled_window.destroy()
            del self.import_job
            ##todo: delete references to widgets

    def src_changed(self,combo,id):
        pass

    def dest_changed(self,combo,id):
        pass

    def import_cancelled(self):
        '''
        called from the import job thread to indicate the job has been cancelled
        '''
        ##todo: give visual indication of cancellation
        self.import_frame.set_sensitive(True)
        self.mode_box.show()
        self.src_combo.set_sensitive(True)
        self.dest_combo.set_sensitive(True)
        self.start_import_button.set_sensitive(True)
        self.start_import_all_button.set_sensitive(True)

    def import_completed(self):
        '''
        called from the import job thread to indicate the job has completed
        '''
        ##todo: give visual indication of completion
        self.import_frame.set_sensitive(True)
        self.mode_box.show()
        self.src_combo.set_sensitive(True)
        self.dest_combo.set_sensitive(True)
        self.start_import_button.set_sensitive(True)
        self.start_import_all_button.set_sensitive(True)

#    def import_now(self,button):
#        self.start_import_button.set_sensitive(False)
#        worker=self.mainframe.tm
#        import_job.start_import(params)

    def cancel_import(self,button):
        self.import_job.import_cancel(False)

    def start_import(self,button,all):
        coll_src=src_combo.get_active_coll()
        coll_dest=dest_combo.get_active_coll()
        if not (coll_src and coll_dest):
            return
        self.start_import_button.set_sensitive(False)
        worker=self.mainframe.tm
        params={}
        params['move']=self.move_radio.get_active()
        params['base_dest_dir']=self.base_dir_entry.get_path()
        params['action_if_exists']=self.exists_combo.get_active()
        iter=self.name_scheme_combo.get_active_iter()
        if iter:
            row=self.name_scheme_model[iter]
            params['dest_name_needs_meta']=row[2]
            params['dest_name_template']=row[1]
        else:
            return
        ij=ImporterImportJob(self,coll_src,coll_dest,params)
        self.mainframe.tm.queue_job_instance(ij)

    def media_connected(self,uri): ##todo: ensure that uri is actually a local path and if so rename the argument
        print 'media connected event for',uri
        sidebar=self.mainframe.sidebar
        sidebar.set_current_page(sidebar.page_num(self.scrolled_window))
        self.mainframe.sidebar_toggle.set_active(True)
        self.src_combo.set_active(id)
#        if self.src_combo.get_editable():
#            self.import_source_combo.set_path(io.get_path_from_uri(uri))

