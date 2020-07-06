from datetime import datetime
import os
from os.path import join, split, splitext
from ..util import *
from .. import util
from .. import config
from .. import shader
from ..report import Report
import tempfile
import shutil
import logging
from itertools import chain

from bpy.props import EnumProperty
from .program import OgreProgram
from bpy_extras import io_utils, node_shader_utils
from mathutils import Vector

def dot_materials(materials, path=None, separate_files=True, prefix='mats', **kwargs):
    """
    generate material files, or copy them into a single file

    path: string - or None if one must use a temp file
    separate_files: bool - each material gets it's own filename
    """
    if not materials:
        logging.debug('WARNING: no materials, not writting .material script')
        return []

    if not path:
        path = tempfile.mkdtemp(prefix='ogre_io')

    if separate_files:
        for mat in materials:
            dot_material(mat, path)
    else:
        mat_file_name = prefix
        target_file = os.path.join(path, '%s.material' % mat_file_name)
        with open(target_file, 'wb') as fd:
            include_missing = False
            for mat in materials:
                if mat is None:
                    include_missing = True
                    continue
                Report.materials.append( material_name(mat) )
                generator = OgreMaterialGenerator(mat, path)
                # Generate before copying textures to collect images first
                material_text = generator.generate()
                if kwargs.get('copy_programs', config.get('COPY_SHADER_PROGRAMS')):
                    generator.copy_programs()
                if kwargs.get('touch_textures', config.get('TOUCH_TEXTURES')):
                    generator.copy_textures()
                fd.write(bytes(material_text+"\n",'utf-8'))
            
            if include_missing:
                fd.write(bytes(MISSING_MATERIAL + "\n",'utf-8'))

def dot_material(mat, path, **kwargs):
    """
    write the material file of a 
    mat: a blender material
    path: target directory to save the file to

    kwargs: 
      * prefix - string. The prefix name of the file. default ''
      * copy_programs - bool. default False
      * touch_textures - bool. Copy the images along to the material files.
    """
    prefix = kwargs.get('prefix', '')
    generator = OgreMaterialGenerator(mat, path, prefix=prefix)
    # Generate before copying textures to collect images first
    material_text = generator.generate()
    if kwargs.get('copy_programs', config.get('COPY_SHADER_PROGRAMS')):
        generator.copy_programs()
    if kwargs.get('touch_textures', config.get('TOUCH_TEXTURES')):
        generator.copy_textures()
    with open(join(path, generator.material_name + ".material"), 'wb') as fd:
        fd.write(bytes(material_text,'utf-8'))

    return generator.material_name

class OgreMaterialGenerator(object):
    # Texture wrapper attribute names
    TEXTURE_KEYS = [
        "base_color_texture",
        "specular_texture",
        "roughness_texture",
        "alpha_texture",
        "normalmap_texture",    # useless ?
        "metallic_texture",
        "emission_color_texture"
    ]
    
    def __init__(self, material, target_path, prefix=''):
        self.material = material
        self.target_path = target_path
        self.w = util.IndentedWriter()
        self.passes = []
        self.material_name = material_name(self.material,prefix=prefix)
        self.images = set()

        if material.node_tree:
            nodes = shader.get_subnodes( self.material.node_tree, type='MATERIAL_EXT' )
            for node in nodes:
                if node.material:
                    self.passes.append( node.material )

    def generate(self):
        self.w.line('// %s generated by blender2ogre %s' % (self.material.name, datetime.now())).nl()
        self.generate_header()
        with self.w.iword('material').word(self.material_name).embed():
            if self.material.shadow_method != "NONE":
                self.w.iline('receive_shadows on')
            else:
                self.w.iline('receive_shadows off')
            with self.w.iword('technique').embed():
                self.generate_passes()

        text = self.w.text
        self.w.text = ''
        return text

    def generate_header(self):
        for mat in self.passes:
            if mat.use_ogre_parent_material and mat.ogre_parent_material:
                usermat = get_ogre_user_material( mat.ogre_parent_material )
                self.w.iline( '// user material: %s' %usermat.name )
                for prog in usermat.get_programs():
                    r.append( prog.data )
                self.w.iline( '// abstract passes //' )
                for line in usermat.as_abstract_passes():
                    self.w.iline(line)

    def generate_passes(self):
        self.generate_pass(self.material)
        for mat in self.passes:
            if mat.use_in_ogre_material_pass: # submaterials
                self.generate_pass(mat)

    def generate_pass( self, mat, pass_name="" ):
        usermat = texnodes = None
        if mat.use_ogre_parent_material:
            usermat = get_ogre_user_material( mat.ogre_parent_material )
            texnodes = shader.get_texture_subnodes( self.material, mat )

        if usermat:
            self.w.iword('pass %s : %s/PASS0' %(pass_name,usermat.name))
        else:
            self.w.iword('pass')
            if pass_name: self.w.word(pass_name)

        with self.w.embed():
            color = mat.diffuse_color
            alpha = 1.0
            if mat.blend_method != "OPAQUE":
                alpha = color[3]

            # Texture wrappers
            textures = {}
            mat_wrapper = node_shader_utils.PrincipledBSDFWrapper(mat)
            for tex_key in self.TEXTURE_KEYS:
                texture = getattr(mat_wrapper, tex_key, None)
                if texture and texture.image:
                    textures[tex_key] = texture
                    # adds image to the list for later copy
                    self.images.add(texture.image)
            
            ## force material alpha to 1.0 if textures use_alpha?
#             usealpha = False #mat.ogre_depth_write
#             for texture in textures:
#                 if (texture.image.alpha_mode != "NONE"):
#                     usealpha = True;
#                     break
            #if usealpha: alpha = 1.0    # let the alpha of the texture control material alpha?
            
            # arbitrary bad translation from PBR to Blinn Phong
            # derive proportions from metallic
            bf = 1.0 - mat.metallic
            mf = max(0.04, mat.metallic)
            # derive specular color
            sc = mathutils.Color(color[:3]) * mf + (1.0 - mf) * mathutils.Color((1, 1, 1)) * (1.0 - mat.roughness)
            si = (1.0 - mat.roughness) * 128

            self.w.iline('diffuse %s %s %s %s' %(color[0]*bf, color[1]*bf, color[2]*bf, alpha) )
            self.w.iline('specular %s %s %s %s %s' % (sc[0], sc[1], sc[2], alpha, si) )

            for name in dir(mat):   #mat.items() - items returns custom props not pyRNA:
                if name.startswith('ogre_') and name != 'ogre_parent_material':
                    var = getattr(mat,name)
                    op = name.replace('ogre_', '')
                    val = var
                    if type(var) == bool:
                        if var: val = 'on'
                        else: val = 'off'
                    self.w.iword(op).word(val).nl()
            self.w.nl()

            if texnodes and usermat.texture_units:
                for i,name in enumerate(usermat.texture_units_order):
                    if i<len(texnodes):
                        node = texnodes[i]
                        if node.texture:
                            geo = shader.get_connected_input_nodes( self.material, node )[0]
                            self.generate_texture_unit( node.texture, name=name, uv_layer=geo.uv_layer )
            elif textures:
                for key, texture in textures.items():
                    self.generate_image_texture_unit(key, texture)

    def generate_image_texture_unit(self, key, texture):
        """
        Generates a texture_unit of a pass.
        
        key: key of the texture in the material wrapper (not used, for normal map if needed)
        texture: the texture wrapper (node_shader_utils.PrincipledBSDFWrapper)
        """
        src_dir = os.path.dirname(bpy.data.filepath)
        # For target path relative
        # dst_dir = os.path.dirname(self.target_path)
        dst_dir = src_dir
        filename = io_utils.path_reference(texture.image.filepath, src_dir, dst_dir, mode='RELATIVE', library=texture.image.library)
        # Do not use if target path relative
        # filename = repr(filepath)[1:-1]
        _, filename = split(filename)
        filename = self.change_ext(filename, texture.image)
        with self.w.iword('texture_unit').embed():
            self.w.iword('texture').word(filename).nl()
            
            exmode = texture.extension
            if exmode in TEXTURE_ADDRESS_MODE:
                self.w.iword('tex_address_mode').word(TEXTURE_ADDRESS_MODE[exmode]).nl()

            if exmode == 'CLIP':
                self.w.iword('tex_border_colour').word(texture.color.r).word(texture.color.g).word(texture.color.b).nl()
            if texture.scale != Vector((1.0, 1.0, 1.0)):
                self.w.iword('scale').real(1.0 / texture.scale.x).real(1.0 / texture.scale.y).nl()
            
            if texture.texcoords == 'Reflection':
                if texture.projection == 'SPHERE':
                    self.w.iline('env_map spherical')
                elif texture.projection == 'FLAT':
                    self.w.iline('env_map planar')
                else: 
                    logging.debug('WARNING: <%s> has a non-UV mapping type (%s) and not picked a proper projection type of: Sphere or Flat' %(texture.name, texture.projection))
            
            if texture.translation.x or texture.translation.y:
                self.w.iword('scroll').real(texture.translation.x).real(texture.translation.y).nl()
            if texture.rotation.z:
                # TODO: how do we set the amount of rotation (here in radians) ?
                self.w.iword('rotate').real(texture.rotation.z).nl()
 
            # TODO: What can we do with normal map ?
            # if key == "normalmap_texture": if texture.material.normalmap_strength != 1.0:
            
            self.w.iword('colour_op').word('modulate').nl()
    
    def generate_texture_unit(self, slot, name=None, uv_layer=None):
        raise NotImplementedError("TODO: slots dont exist anymore - use image")
        
        if not slot.use:
            return
        texture = slot.texture
        if not hasattr(texture, 'image'):
            logging.warn('texture must be of type IMAGE', texture)
            return
        if not texture.image:
            logging.warn('texture has no image assigned', texture)
            return

        _alphahack = None
        if not name:
            name = ''
        _, filename = split(util.texture_image_path(texture))
        filename = self.change_ext(filename, texture.image)
        with self.w.iword('texture_unit').word(name).embed():
            self.w.iword('texture').word(filename).nl()

            exmode = texture.extension
            if exmode in TEXTURE_ADDRESS_MODE:
                self.w.iword('tex_address_mode').word(TEXTURE_ADDRESS_MODE[exmode]).nl()

            if exmode == 'CLIP':
                self.w.iword('tex_border_colour').word(slot.color.r).word(slot.color.g).word(slot.color.b).nl()
            self.w.iword('scale').real(1.0/slot.scale.x).real(1.0/slot.scale.y).nl()
            if slot.texture_coords == 'REFLECTION':
                if slot.mapping == 'SPHERE':
                    self.w.iword('env_map spherical').nl()
                elif slot.mapping == 'FLAT':
                    self.w.iword('env_map planar').nl()
                else: 
                    logging.debug('WARNING: <%s> has a non-UV mapping type (%s) and not picked a proper projection type of: Sphere or Flat' %(texture.name, slot.mapping))

            ox,oy,oz = slot.offset
            if ox or oy:
                self.w.iword('scroll').real(ox).real(oy).nl()
            if oz:
                self.w.iword('rotate').real(oz).nl()

            if slot.use_from_dupli:    # hijacked again - june7th
                self.w.iword('rotate_anim').real(slot.density_factor).real(slot.emission_factor).nl()
            if slot.use_map_scatter:    # hijacked from volume shaders
                self.w.iword('scroll_anim').real(slot.density_factor).real(slot.emission_factor).nl()

            if slot.uv_layer:
                idx = find_uv_layer_index( slot.uv_layer, self.material )
                self.w.iword('tex_coord_set').integer(idx).nl()

            rgba = False
            if texture.image.depth == 32: rgba = True
            btype = slot.blend_type     # TODO - fix this hack if/when slots support pyRNA
            ex = False; texop = None
            if btype in TEXTURE_COLOUR_OP:
                if btype=='MIX' and \
                    ((slot.use_map_alpha and not slot.use_stencil) or \
                     (slot.texture.use_alpha and slot.texture.image.use_alpha and not slot.use_map_alpha)):
                    if slot.diffuse_color_factor >= 1.0:
                        texop = 'alpha_blend'
                    else:
                        texop = TEXTURE_COLOUR_OP[ btype ]
                        ex = True
                elif btype=='MIX' and slot.use_map_alpha and slot.use_stencil:
                    texop = 'blend_current_alpha'; ex=True
                elif btype=='MIX' and not slot.use_map_alpha and slot.use_stencil:
                    texop = 'blend_texture_alpha'; ex=True
                else:
                    texop = TEXTURE_COLOUR_OP[ btype ]
            elif btype in TEXTURE_COLOUR_OP_EXcolour_op_ex:
                    texop = TEXTURE_COLOUR_OP_EX[ btype ]
                    ex = True

            if texop and ex:
                if texop == 'blend_manual':
                    factor = 1.0 - slot.diffuse_color_factor
                    self.w.iword('colour_op_ex').word(texop).word('src_texture src_current').word(factor).nl()
                else:
                    self.w.iword('colour_op_ex').word(texop).word('src_texture src_current').nl()
            elif texop:
                    self.w.iword('colour_op').word(texop).nl()
            else:
                if uv_layer:
                    idx = find_uv_layer_index( uv_layer )
                    self.w.iword('tex_coord_set').integer(idx)

    def copy_textures(self):
        for image in self.images:
            self.copy_texture(image)

    def copy_texture(self, image):
        origin_filepath = image.filepath
        target_filepath = split(origin_filepath)[1]
        target_filepath = self.change_ext(target_filepath, image)
        target_filepath = join(self.target_path, target_filepath)
        
        if image.packed_file:
            # packed in .blend file, save image as target file
            image.filepath = target_filepath
            image.save()
            image.filepath = origin_filepath
            logging.info("copy (%s)", origin_filepath)
        else:
            image_filepath = bpy.path.abspath(image.filepath, library=image.library)
            image_filepath = os.path.normpath(image_filepath)
            
            # Should we update the file
            update = False
            if os.path.isfile(target_filepath):
                src_stat = os.stat(target_filepath)
                dst_stat = os.stat(image_filepath)
                update = src_stat.st_size != dst_stat.st_size \
                    or src_stat.st_mtime != dst_stat.st_mtime
            else:
                update = True
                
            if (update):
                # copy2 tries to copy all metadata (modification date included), to keep update decision consistent
                shutil.copy2(image_filepath, target_filepath)
                logging.info("copy (%s)", origin_filepath)
            else:
                logging.info("skip copy (%s). texture is already up to date.", origin_filepath)

    def get_active_programs(self):
        r = []
        for mat in self.passes:
            if mat.use_ogre_parent_material and mat.ogre_parent_material:
                usermat = get_ogre_user_material( mat.ogre_parent_material )
                for prog in usermat.get_programs(): r.append( prog )
        return r

    def copy_programs(self):
        for prog in self.get_active_programs():
            if prog.source:
                prog.save(self.target_path)
            else:
                logging.warn('uses program %s which has no source' % (prog.name))

    def change_ext( self, name, image ):
        name_no_ext, _ = splitext(name)
        if image.file_format != 'NONE':
            name = name_no_ext + "." + image.file_format
        if config.get('FORCE_IMAGE_FORMAT') != 'NONE':
            name = name_no_ext + "." + config.get('FORCE_IMAGE_FORMAT')
        return name

# Make default material for missing materials:
# * Red flags for users so they can quickly see what they forgot to assign a material to.
# * Do not crash if no material on object - thats annoying for the user.
TEXTURE_COLOUR_OP = {
    'MIX'       :   'modulate',        # Ogre Default - was "replace" but that kills lighting
    'ADD'     :   'add',
    'MULTIPLY' : 'modulate',
    #'alpha_blend' : '',
}
TEXTURE_COLOUR_OP_EX = {
    'MIX'       :    'blend_manual',
    'SCREEN': 'modulate_x2',
    'LIGHTEN': 'modulate_x4',
    'SUBTRACT': 'subtract',
    'OVERLAY':    'add_signed',
    'DIFFERENCE': 'dotproduct',        # best match?
    'VALUE': 'blend_diffuse_colour',
}

TEXTURE_ADDRESS_MODE = {
    'REPEAT': 'wrap',
    'EXTEND': 'clamp',
    'CLIP'  : 'border',
    'CHECKER' : 'mirror'
}


MISSING_MATERIAL = '''
material _missing_material_
{
    receive_shadows off
    technique
    {
        pass
        {
            ambient 0.1 0.1 0.1 1.0
            diffuse 0.8 0.0 0.0 1.0
            specular 0.5 0.5 0.5 1.0 12.5
            emissive 0.3 0.3 0.3 1.0
        }
    }
}
'''

def load_user_materials():
    # I think this is soley used for realxtend... the config of USER_MATERIAL
    # points to a subdirectory of tundra by default. In this case all parsing
    # can be moved to the tundra subfolder
    if os.path.isdir( config.get('USER_MATERIALS') ):
        scripts,progs = update_parent_material_path( config.get('USER_MATERIALS') )
        for prog in progs:
            logging.info('Ogre shader program ' + prog.name)


def material_name( mat, clean = False, prefix='' ):
    """
    returns the material name.

    materials from a library might be exported several times for multiple objects.
    there is no need to have those textures + material scripts several times. thus
    library materials are prefixed with the material filename. (e.g. test.blend + diffuse
    should result in "test_diffuse". special chars are converted to underscore.

    clean: deprecated. do not use!
    """
    if type(mat) is str:
        return prefix + clean_object_name(mat)
    name = clean_object_name(mat.name)
    if mat.library:
        _, filename = split(mat.library.filepath)
        prefix, _ = splitext(filename)
        return prefix + "_" + name
    else:
        return prefix + name

def get_shader_program( name ):
    if name in OgreProgram.PROGRAMS:
        return OgreProgram.PROGRAMS[ name ]
    else:
        logging.debug('WARNING: no shader program named: %s' %name)

def get_shader_programs():
    return OgreProgram.PROGRAMS.values()

def parse_material_and_program_scripts( path, scripts, progs, missing ):   # recursive
    for name in os.listdir(path):
        url = os.path.join(path,name)
        if os.path.isdir( url ):
            parse_material_and_program_scripts( url, scripts, progs, missing )

        elif os.path.isfile( url ):
            if name.endswith( '.material' ):
                logging.debug( '<found material> ' + url )
                scripts.append( MaterialScripts( url ) )

            if name.endswith('.program'):
                logging.debug( '<found program> ' + url )
                data = open( url, 'rb' ).read().decode('utf-8')

                chk = []; chunks = [ chk ]
                for line in data.splitlines():
                    line = line.split('//')[0]
                    if line.startswith('}'):
                        chk.append( line )
                        chk = []; chunks.append( chk )
                    elif line.strip():
                        chk.append( line )

                for chk in chunks:
                    if not chk: continue
                    p = OgreProgram( data='\n'.join(chk) )
                    if p.source:
                        ok = p.reload()
                        if not ok: missing.append( p )
                        else: progs.append( p )

def get_ogre_user_material( name ):
    if name in MaterialScripts.ALL_MATERIALS:
        return MaterialScripts.ALL_MATERIALS[ name ]

class OgreMaterialScript(object):
    def get_programs(self):
        progs = []
        for name in list(self.vertex_programs.keys()) + list(self.fragment_programs.keys()):
            p = get_shader_program( name )  # OgreProgram.PROGRAMS
            if p: progs.append( p )
        return progs

    def __init__(self, txt, url):
        self.url = url
        self.data = txt.strip()
        self.parent = None
        self.vertex_programs = {}
        self.fragment_programs = {}
        self.texture_units = {}
        self.texture_units_order = []
        self.passes = []

        line = self.data.splitlines()[0]
        assert line.startswith('material')
        if ':' in line:
            line, self.parent = line.split(':')
        self.name = line.split()[-1]
        logging.debug( 'new ogre material: %s' %self.name )

        brace = 0
        self.techniques = techs = []
        prog = None  # pick up program params
        tex = None  # pick up texture_unit options, require "texture" ?
        for line in self.data.splitlines():
            #logging.debug( line )
            rawline = line
            line = line.split('//')[0]
            line = line.strip()
            if not line: continue

            if line == '{': brace += 1
            elif line == '}': brace -= 1; prog = None; tex = None

            if line.startswith( 'technique' ):
                tech = {'passes':[]}; techs.append( tech )
                if len(line.split()) > 1: tech['technique-name'] = line.split()[-1]
            elif techs:
                if line.startswith('pass'):
                    P = {'texture_units':[], 'vprogram':None, 'fprogram':None, 'body':[]}
                    tech['passes'].append( P )
                    self.passes.append( P )

                elif tech['passes']:
                    P = tech['passes'][-1]
                    P['body'].append( rawline )

                    if line == '{' or line == '}': continue

                    if line.startswith('vertex_program_ref'):
                        prog = P['vprogram'] = {'name':line.split()[-1], 'params':{}}
                        self.vertex_programs[ prog['name'] ] = prog
                    elif line.startswith('fragment_program_ref'):
                        prog = P['fprogram'] = {'name':line.split()[-1], 'params':{}}
                        self.fragment_programs[ prog['name'] ] = prog

                    elif line.startswith('texture_unit'):
                        prog = None
                        tex = {'name':line.split()[-1], 'params':{}}
                        if tex['name'] == 'texture_unit': # ignore unnamed texture units
                            logging.debug('WARNING: material %s contains unnamed texture_units' %self.name)
                            logging.debug('---unnamed texture units will be ignored---')
                        else:
                            P['texture_units'].append( tex )
                            self.texture_units[ tex['name'] ] = tex
                            self.texture_units_order.append( tex['name'] )

                    elif prog:
                        p = line.split()[0]
                        if p=='param_named':
                            items = line.split()
                            if len(items) == 4: p, o, t, v = items
                            elif len(items) == 3:
                                p, o, v = items
                                t = 'class'
                            elif len(items) > 4:
                                o = items[1]; t = items[2]
                                v = items[3:]

                            opt = { 'name': o, 'type':t, 'raw_value':v }
                            prog['params'][ o ] = opt
                            if t=='float': opt['value'] = float(v)
                            elif t in 'float2 float3 float4'.split(): opt['value'] = [ float(a) for a in v ]
                            else: logging.debug('unknown type:', t)

                    elif tex:   # (not used)
                        tex['params'][ line.split()[0] ] = line.split()[ 1 : ]

        for P in self.passes:
            lines = P['body']
            while lines and ''.join(lines).count('{')!=''.join(lines).count('}'):
                if lines[-1].strip() == '}': lines.pop()
                else: break
            P['body'] = '\n'.join( lines )
            assert P['body'].count('{') == P['body'].count('}')     # if this fails, the parser choked

        #logging.debug( self.techniques )
        self.hidden_texture_units = rem = []
        for tex in self.texture_units.values():
            if 'texture' not in tex['params']:
                rem.append( tex )
        for tex in rem:
            logging.debug('WARNING: not using texture_unit because it lacks a "texture" parameter ' + tex['name'])
            self.texture_units.pop( tex['name'] )

        if len(self.techniques)>1:
            logging.debug('WARNING: user material %s has more than one technique' %self.url)

    def as_abstract_passes( self ):
        r = []
        for i,P in enumerate(self.passes):
            head = 'abstract pass %s/PASS%s' %(self.name,i)
            r.append( head + '\n' + P['body'] )
        return r

class MaterialScripts(object):
    ALL_MATERIALS = {}
    ENUM_ITEMS = []

    def __init__(self, url):
        self.url = url
        self.data = ''
        data = open( url, 'rb' ).read()
        try:
            self.data = data.decode('utf-8')
        except:
            self.data = data.decode('latin-1')

        self.materials = {}
        ## chop up .material file, find all material defs ####
        mats = []
        mat = []
        skip = False    # for now - programs must be defined in .program files, not in the .material
        for line in self.data.splitlines():
            if not line.strip(): continue
            a = line.split()[0]             #NOTE ".split()" strips white space
            if a == 'material':
                mat = []; mats.append( mat )
                mat.append( line )
            elif a in ('vertex_program', 'fragment_program', 'abstract'):
                skip = True
            elif mat and not skip:
                mat.append( line )
            elif skip and line=='}':
                skip = False

        ##########################
        for mat in mats:
            omat = OgreMaterialScript( '\n'.join( mat ), url )
            if omat.name in self.ALL_MATERIALS:
                logging.debug( 'WARNING: material %s redefined' %omat.name )
                #logging.debug( '--OLD MATERIAL--')
                #logging.debug( self.ALL_MATERIALS[ omat.name ].data )
                #logging.debug( '--NEW MATERIAL--')
                #logging.debug( omat.data )
            self.materials[ omat.name ] = omat
            self.ALL_MATERIALS[ omat.name ] = omat
            if omat.vertex_programs or omat.fragment_programs:  # ignore materials without programs
                self.ENUM_ITEMS.append( (omat.name, omat.name, url) )

    @classmethod # only call after parsing all material scripts
    def reset_rna(self, callback=None):
        bpy.types.Material.ogre_parent_material = EnumProperty(
            name="Script Inheritence",
            description='ogre parent material class',
            items=self.ENUM_ITEMS,
            #update=callback
        )

IMAGE_FORMATS =  [
    ('NONE','NONE', 'do not convert image'),
    ('bmp', 'bmp', 'bitmap format'),
    ('jpg', 'jpg', 'jpeg format'),
    ('gif', 'gif', 'gif format'),
    ('png', 'png', 'png format'),
    ('tga', 'tga', 'targa format'),
    ('dds', 'dds', 'dds format'),
]

def is_image_postprocessed( image ):
    return False
    if config.get('FORCE_IMAGE_FORMAT') != 'NONE' or image.use_resize_half or image.use_resize_absolute or image.use_color_quantize or image.use_convert_format:
        return True
    else:
        return False


def update_parent_material_path( path ):
    ''' updates RNA '''
    logging.debug( '>>SEARCHING FOR OGRE MATERIALS: %s' %path )
    scripts = []
    progs = []
    missing = []
    parse_material_and_program_scripts( path, scripts, progs, missing )

    if missing:
        logging.debug('WARNING: missing shader programs:')
        for p in missing: logging.debug(p.name)
    if missing and not progs:
        logging.debug('WARNING: no shader programs were found - set "SHADER_PROGRAMS" to your path')

    MaterialScripts.reset_rna( callback=shader.on_change_parent_material )
    return scripts, progs
