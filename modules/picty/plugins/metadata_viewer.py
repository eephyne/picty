'''

    picty - Metadata Viewer Plugin
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

import gtk
import datetime

from picty import imagemanip
from picty import settings
from picty import pluginbase
from picty import metadata

class MetaDataViewer(pluginbase.Plugin):
    name='MetadataViewer'
    display_name='Metadata Viewer'
    api_version='0.1.0'
    version='0.1.0'
    def __init__(self):
        self.item=None
    def plugin_init(self,mainframe,app_init):
        #register a button in the viewer to enter metadata mode
        self.cancel_button=gtk.Button("_Close")
        self.cancel_button.connect("clicked",self.metadata_cancel_callback)
        self.button_save=gtk.Button("Save",gtk.STOCK_SAVE)
        self.button_revert=gtk.Button("Revert",gtk.STOCK_REVERT_TO_SAVED)
        self.button_save.set_sensitive(False)
        self.button_revert.set_sensitive(False)
        self.button_save.connect("clicked",self.metadata_save)
        self.button_revert.connect("clicked",self.metadata_revert)
        buttons=gtk.HBox()
#        buttons.pack_start(self.button_revert,True,False)
#        buttons.pack_start(self.button_save,True,False)
        buttons.pack_start(self.cancel_button,True,False)

        self.viewer=mainframe.iv
        self.meta_table=self.create_meta_table()
        self.meta_box=gtk.VBox()
        self.meta_box.pack_start(self.meta_table,True)
        self.meta_box.pack_start(buttons,False)
        self.meta_box.show_all()


    def plugin_shutdown(self,app_shutdown=False):
        if self.item!=None:
            self.viewer.vpane.remove(self.meta_box)
        self.item=None

    def viewer_register_shortcut(self,shortcut_toolbar):
        '''
        called by the framework to register shortcut on mouse over commands
        append a tuple containing the shortcut commands
        '''
        shortcut_toolbar.register_tool_for_plugin(self,'Metadata Viewer',self.metadata_button_callback,shortcut_toolbar.default_update_callback,[gtk.STOCK_INFO],'View detailed metadata for this image',priority=45)

    def viewer_item_opening(self,item):
        if self.item!=None and self.item!=item:
            self.item=item
            self.update_meta_table(item)
        return True

    def metadata_button_callback(self,cmd):
        item=self.viewer.item
        self.viewer.vpane.add2(self.meta_box)
        self.update_meta_table(item)
        self.item=item
        self.viewer.imarea.grab_focus()

    def metadata_cancel_callback(self,widget):
        self.viewer.vpane.remove(self.meta_box)
        self.item=None
        self.viewer.imarea.grab_focus()

    ##TODO: NEED TO IMPLEMENT A PLUGIN METHOD FOR HANDLING ITEM CHANGE EVENT IN THE VIEWER SO TABLE CAN BE UPDATED

    def add_meta_row(self,table,data_items,key,label,data,row,writable=False):
        child1=gtk.Label(label)
        align=gtk.Alignment(0,0.5,0,0)
        align.add(child1)
        table.attach(align, left_attach=0, right_attach=1, top_attach=row, bottom_attach=row+1,
               xoptions=gtk.FILL, yoptions=gtk.EXPAND|gtk.FILL, xpadding=0, ypadding=0)
        child2=gtk.Label(data)
        align=gtk.Alignment(0,0.5,0,0)
        align.add(child2)
#        if writable:
#            child2=gtk.Entry()
#            child2.set_text(data)
#            child2.connect("changed",self.metadata_changed,key)
#        else:
#            child2=gtk.Label(data)
        table.attach(align, left_attach=1, right_attach=2, top_attach=row, bottom_attach=row+1,
               xoptions=gtk.EXPAND|gtk.FILL, yoptions=gtk.EXPAND|gtk.FILL, xpadding=0, ypadding=0)
        data_items[key]=(child1,child2)

    def create_meta_table(self):
        rows=2
        rows+=len(metadata.apptags)
        stable=gtk.ScrolledWindow()
        stable.set_policy(gtk.POLICY_NEVER,gtk.POLICY_AUTOMATIC)
        table = gtk.Table(rows=rows, columns=2, homogeneous=False)
        stable.data_items=dict()
        self.add_meta_row(table, stable.data_items,'FullPath','Full Path','',0)
        self.add_meta_row(table, stable.data_items,'UnixLastModified','Last Modified','',1)
        r=2
        for t in metadata.apptags:
            k=t[0]
            v=t[1]
            w=t[2]
            try:
                self.add_meta_row(table,stable.data_items,k,v,'',r,w)
            except:
                None
            r+=1
        table.show_all()
        stable.add_with_viewport(table)
        stable.set_focus_chain(tuple())
        return stable

    def update_meta_table(self,item):
        self.change_block=True
        try:
            enable=item.is_meta_changed()
            self.button_save.set_sensitive(enable)
            self.button_revert.set_sensitive(enable)
        except:
            self.button_save.set_sensitive(False)
            self.button_revert.set_sensitive(False)
        self.meta_table.data_items['FullPath'][1].set_text(self.viewer.browser.active_collection.get_path(item))
        d=datetime.datetime.fromtimestamp(item.mtime)
        self.meta_table.data_items['UnixLastModified'][1].set_text(d.isoformat(' '))
        for t in metadata.apptags:
            k=t[0]
            v=t[1]
            w=t[2]
            value=''
            if item.meta==None:
                self.meta_table.data_items[k][1].set_text('')
            else:
                try:
                    value=item.meta[k]
                    value=metadata.app_key_to_string(k,value)
                except:
                    value=''
                try:
                    self.meta_table.data_items[k][1].set_text(value)
                except:
                    print 'error updating meta table'
                    print 'values',value,type(value)
        self.change_block=False

    def is_meta_changed(self):
        return 'meta_backup' in self.__dict__

    def metadata_changed(self,widget,key):
        if self.change_block:
            return
        enable=self.item.set_meta_key(key,widget.get_text())
        self.button_save.set_sensitive(enable)
        self.button_revert.set_sensitive(enable)
        print key,widget.get_text()

    def metadata_save(self,widget):
        item=self.item
        if item.is_meta_changed():
            self.viewer.browser.active_collection.write_metadata(item)
        self.button_save.set_sensitive(False)
        self.button_revert.set_sensitive(False)
        self.update_meta_table(item)

    def metadata_revert(self,widget):
        item=self.item
        if not item.is_meta_changed():
            return
        try:
            orient=item.meta['Orientation']
        except:
            orient=None
        try:
            orient_backup=item.meta_backup['Orientation']
        except:
            orient_backup=None
        item.meta_revert(self.viewer.browser.active_collection)
        ##todo: need to recreate thumb if orientation changed
        if orient!=orient_backup:
            item.thumb=None
            self.worker.recreate_thumb(item)
            self.SetItem(item)
        self.button_save.set_sensitive(False)
        self.button_revert.set_sensitive(False)
        self.update_meta_table(item)

    def create_metadata_frame(self):
        item=self.item
        rows=2
        d=datetime.datetime.fromtimestamp(item.mtime)

        if item.meta!=None:
            rows+=len(metadata.apptags)
        stable=gtk.ScrolledWindow()
        stable.set_policy(gtk.POLICY_AUTOMATIC,gtk.POLICY_AUTOMATIC)
        table = gtk.Table(rows=rows, columns=2, homogeneous=False)
        self.add_meta_row(table,'Full Path',item.uid,0)
        self.add_meta_row(table,'Last Modified',d.isoformat(' '),1)
        r=2
        for t in metadata.apptags:
            k=t[0]
            v=t[1]
            w=t[2]
            try:
                self.add_meta_row(table,v,str(item.meta[k]),r)
            except:
                None
            r+=1
        table.show_all()
        stable.add_with_viewport(table)
        return stable

