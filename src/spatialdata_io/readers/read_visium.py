from spatialdata import SpatialData, Scale
import scanpy as sc
import numpy as np
from xarray import DataArray
from typing import Optional


def read_visium(path: str, coordinate_system_name: Optional[str] = None) -> SpatialData:
    """
    Read Visium data from a directory containing the output of the spaceranger pipeline.

    Parameters
    ----------
    path : str
        Path to the directory containing the output of the spaceranger pipeline.
    coordinate_system_name : str, optional
        Name of the coordinate system to use. If not provided, it's the name of the library found in the .h5 matrix

    Returns
    -------
    SpatialData
        SpatialData object containing the data from the Visium experiment.
    """
    from spatialdata_io.constructors.table import table_update_anndata
    from spatialdata_io.constructors.circles import circles_anndata_from_coordinates

    adata = sc.read_visium(path)
    libraries = list(adata.uns["spatial"].keys())
    assert len(libraries) == 1
    lib = libraries[0]
    if coordinate_system_name is None:
        coordinate_system_name = lib
    csn = coordinate_system_name

    # expression table
    expression = adata.copy()
    del expression.uns
    del expression.obsm
    expression.obs_names_make_unique()
    expression.var_names_make_unique()
    table_update_anndata(
        expression,
        regions=f"/points/{csn}",
        regions_key="library_id",
        instance_key="visium_spot_id",
        regions_values=f"/points/{csn}",
        instance_values=np.arange(len(adata)),
    )

    # circles ("visium spots")
    radius = adata.uns["spatial"][lib]["scalefactors"]["spot_diameter_fullres"] / 2
    circles = circles_anndata_from_coordinates(
        coordinates=adata.obsm["spatial"],
        radii=radius,
        instance_key="visium_spot_id",
        instance_values=np.arange(len(adata)),
    )

    # image
    img = DataArray(adata.uns["spatial"][lib]["images"]["hires"], dims=("y", "x", "c"))
    assert img.dtype == np.float32 and np.min(img) >= 0. and np.max(img) <= 1.
    img = (img * 255).astype(np.uint8)

    # transformation
    scale_factors = np.array([1.0] + [1 / adata.uns["spatial"][lib]["scalefactors"]["tissue_hires_scalef"]] * 2)
    transform = Scale(scale=scale_factors)

    sdata = SpatialData(
        images={csn: img},
        points={csn: circles},
        table=expression,
        transformations={(f"/images/{csn}", csn): transform, (f"/points/{csn}", csn): None},
    )
    return sdata