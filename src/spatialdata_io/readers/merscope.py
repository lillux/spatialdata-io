import re
from pathlib import Path
from typing import Any, Optional, Union

import anndata
import dask.dataframe as dd
import geopandas
import numpy as np
import pandas as pd
from dask import array as da
from dask_image.imread import imread
from spatialdata import SpatialData
from spatialdata.models import Image3DModel, PointsModel, ShapesModel, TableModel
from spatialdata.transformations import Affine, Identity

from spatialdata_io._constants._constants import MerscopeKeys
from spatialdata_io._docs import inject_docs


def _scan_images(images_dir: Path) -> tuple[list[str], list[str]]:
    """
    Gets images names inside a directory

    It returns all the different channels (stainings) and all the z-levels (usually 0...6)
    """
    exp = r"mosaic_(?P<stain>[\w|-]+[0-9]?)_z(?P<z>[0-9]+).tif"
    matches = [re.search(exp, file.name) for file in images_dir.iterdir()]

    stainings = {match.group("stain") for match in matches if match}
    z_levels = {match.group("z") for match in matches if match}

    return list(stainings), list(z_levels)


def _get_file_paths(path: Path, vpt_outputs: Optional[Union[Path, str, dict[str, Any]]]) -> tuple[Path, Path, Path]:
    """
    Gets the MERSCOPE file paths when vpt_outputs is provided

    That is, (i) the file of transcript per cell, (ii) the cell metadata file, and (iii) the cell boundary file
    """
    if vpt_outputs is None:
        return (
            path / MerscopeKeys.COUNTS_FILE,
            path / MerscopeKeys.CELL_METADATA_FILE,
            path / MerscopeKeys.BOUNDARIES_FILE,
        )

    if isinstance(vpt_outputs, str) or isinstance(vpt_outputs, Path):
        vpt_outputs = Path(vpt_outputs)

        plausible_boundaries = [
            vpt_outputs / MerscopeKeys.CELLPOSE_BOUNDARIES,
            vpt_outputs / MerscopeKeys.WATERSHED_BOUNDARIES,
        ]
        valid_boundaries = [path for path in plausible_boundaries if path.exists()]

        assert (
            valid_boundaries
        ), f"Boundary file not found - expected to find one of these files: {', '.join(map(str, plausible_boundaries))}"

        return (
            vpt_outputs / MerscopeKeys.COUNTS_FILE,
            vpt_outputs / MerscopeKeys.CELL_METADATA_FILE,
            valid_boundaries[0],
        )

    if isinstance(vpt_outputs, dict):
        return (
            vpt_outputs[MerscopeKeys.VPT_NAME_COUNTS],
            vpt_outputs[MerscopeKeys.VPT_NAME_OBS],
            vpt_outputs[MerscopeKeys.VPT_NAME_BOUNDARIES],
        )

    raise ValueError(
        f"`vpt_outputs` has to be either `None`, `str`, `Path`, or `dict`. Found type {type(vpt_outputs)}."
    )


@inject_docs(ms=MerscopeKeys)
def merscope(
    path: Union[str, Path], vpt_outputs: Optional[Union[Path, str, dict[str, Any]]] = None, read_tif: bool = True
) -> SpatialData:
    """
    Read *MERSCOPE* data from Vizgen.

    This function reads the following files:

        - ``{ms.COUNTS_FILE!r}``: Counts file.
        - ``{ms.TRANSCRIPTS_FILE!r}``: Transcript file.
        - ``{ms.CELL_METADATA_FILE!r}``: Per-cell metadata file.
        - ``{ms.BOUNDARIES_FILE!r}``: Cell polygon boundaries.
        - `mosaic_**_z*.tif` images inside the ``{ms.IMAGES_DIR!r}`` directory.

    Parameters
    ----------
    path
        Path to the root directory containing the *Merscope* files (e.g., `detected_transcripts.csv`).
    vpt_outputs
        Optional arguments to indicate the output of the vizgen-postprocessing-tool (VPT), when used.
        If a folder path is provided, it looks inside the folder for the following files:
        ``{ms.COUNTS_FILE!r}``, ``{ms.CELL_METADATA_FILE!r}``, and a boundary parquet file.
        If a dictionnary, then the following keys can be provided:
        ``{ms.VPT_NAME_COUNTS!r}``, ``{ms.VPT_NAME_OBS!r}``, ``{ms.VPT_NAME_BOUNDARIES!r}`` with the desired path as the value.
    read_tif
        Whether to read the tif images or not

    Returns
    -------
    :class:`spatialdata.SpatialData`
    """
    path = Path(path)
    count_path, obs_path, boundaries_path = _get_file_paths(path, vpt_outputs)
    images_dir = path / MerscopeKeys.IMAGES_DIR

    microns_to_pixels = np.genfromtxt(images_dir / MerscopeKeys.TRANSFORMATION_FILE)
    microns_to_pixels = Affine(microns_to_pixels, input_axes=("x", "y"), output_axes=("x", "y"))

    # Images
    images = {}

    if read_tif:
        stainings, z_levels = _scan_images(images_dir)
        for z_level in z_levels:
            im = da.stack(
                [imread(images_dir / f"mosaic_{stain}_z{z_level}.tif").squeeze() for stain in stainings], axis=0
            )
            parsed_im = Image3DModel.parse(
                im,
                dims=("c", "z", "y", "x"),
                transformations={"pixels": Identity()},
                c_coords=stainings,
            )
            images[f"z{z_level}"] = parsed_im

    # Transcripts
    transcript_df = dd.read_csv(path / MerscopeKeys.TRANSCRIPTS_FILE)
    transcripts = PointsModel.parse(
        transcript_df,
        coordinates={"x": MerscopeKeys.GLOBAL_X, "y": MerscopeKeys.GLOBAL_Y, "z": MerscopeKeys.GLOBAL_Z},
        transformations={"pixels": Identity()},
    )
    points = {}
    gene_categorical = dd.from_pandas(
        transcripts["gene"].compute().astype("category"), npartitions=transcripts.npartitions
    ).reset_index(drop=True)
    transcripts["gene"] = gene_categorical

    # split the transcripts into the different z-levels
    z = transcripts["z"].compute()
    z_levels = z.value_counts().index
    z_levels = sorted(z_levels, key=lambda x: int(x))
    for z_level in z_levels:
        transcripts_subset = transcripts[z == z_level]
        # temporary solution until the 3D support is better developed
        transcripts_subset = transcripts_subset.drop("z", axis=1)
        points[f"transcripts_z{int(z_level)}"] = transcripts_subset

    # Polygons
    geo_df = geopandas.read_parquet(boundaries_path)
    geo_df = geo_df.rename_geometry("geometry")
    geo_df = geo_df[geo_df[MerscopeKeys.Z_INDEX] == 0]  # Avoid duplicate boundaries on all z-levels
    geo_df.index = geo_df[MerscopeKeys.INSTANCE_KEY].astype(str)

    polygons = ShapesModel.parse(geo_df, transformations={"pixels": microns_to_pixels})
    shapes = {"polygons": polygons}

    # Table
    data = pd.read_csv(count_path, index_col=0, dtype={MerscopeKeys.COUNTS_CELL_KEY: str})
    obs = pd.read_csv(obs_path, index_col=0, dtype={MerscopeKeys.INSTANCE_KEY: str})

    is_gene = ~data.columns.str.lower().str.contains("blank")
    adata = anndata.AnnData(data.loc[:, is_gene], dtype=data.values.dtype, obs=obs)

    adata.obsm["blank"] = data.loc[:, ~is_gene]  # blank fields are excluded from adata.X
    adata.obsm["spatial"] = adata.obs[[MerscopeKeys.CELL_X, MerscopeKeys.CELL_Y]].values
    adata.obs["region"] = pd.Series(path.stem, index=adata.obs_names, dtype="category")
    adata.obs[MerscopeKeys.INSTANCE_KEY] = adata.obs.index

    table = TableModel.parse(
        adata,
        region_key="region",
        region=adata.obs["region"].cat.categories.tolist(),
        instance_key=MerscopeKeys.INSTANCE_KEY.value,
    )

    return SpatialData(shapes=shapes, points=points, images=images, table=table)
