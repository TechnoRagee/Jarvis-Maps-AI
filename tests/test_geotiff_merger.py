"""
Unit and Integration Tests for GeoTIFF Merger & Adaptive Rescaling
"""

import os
import glob
import unittest
import numpy as np
from PIL import Image, TiffImagePlugin
from utils.geotiff_merger import (
    GeoTIFFMetadata,
    read_geotiff,
    read_geotiff_metadata,
    compute_edge_weights,
    merge_geotiffs,
    enhance_geotiff_lines,
)

class TestGeoTIFFMerger(unittest.TestCase):
    def setUp(self):
        self.test_dir = os.path.join(os.path.dirname(__file__), "scratch", "test_geotiff_out")
        os.makedirs(self.test_dir, exist_ok=True)

    def _create_mock_geotiff(self, path, width=200, height=100, origin_lng=77.0, origin_lat=28.5, scale_x=0.0001, scale_y=0.0001):
        """Creates a small mock GeoTIFF with EPSG:4326 tags."""
        img = np.full((height, width, 3), 120, dtype=np.uint8)
        # Add a mock red boundary line
        img[height // 2, :, :] = [255, 20, 20]
        
        pil_img = Image.fromarray(img, mode="RGB")
        tags = TiffImagePlugin.ImageFileDirectory_v2()
        tags[33550] = (float(scale_x), -float(scale_y), 0.0)
        tags[33922] = (0.0, 0.0, 0.0, float(origin_lng), float(origin_lat), 0.0)
        tags[34735] = (1, 1, 0, 3, 1024, 0, 1, 2, 1025, 0, 1, 1, 2048, 0, 1, 4326)
        
        pil_img.save(path, tiffinfo=tags)

    def test_metadata_extraction(self):
        mock_path = os.path.join(self.test_dir, "mock_meta.tif")
        self._create_mock_geotiff(mock_path, width=300, height=150, origin_lng=77.10, origin_lat=28.60)
        
        meta = read_geotiff_metadata(mock_path)
        self.assertEqual(meta.width, 300)
        self.assertEqual(meta.height, 150)
        self.assertAlmostEqual(meta.origin_x, 77.10)
        self.assertAlmostEqual(meta.origin_y, 28.60)
        self.assertAlmostEqual(meta.scale_x, 0.0001)
        self.assertAlmostEqual(meta.scale_y, 0.0001)

    def test_10_plus_tiles_merge_with_adaptive_rescaling(self):
        """Simulates 12 neighbouring tiles spanning across a wide area that would exceed max_canvas_dim."""
        tile_paths = []
        for i in range(12):
            p = os.path.join(self.test_dir, f"tile_{i}.tif")
            # Step in longitude by 0.01 degrees each
            self._create_mock_geotiff(
                p,
                width=500,
                height=300,
                origin_lng=77.00 + i * 0.008,
                origin_lat=28.60,
                scale_x=0.00002,  # Fine resolution
                scale_y=0.00002,
            )
            tile_paths.append(p)

        out_path = os.path.join(self.test_dir, "merged_12_tiles.tif")
        # Set max_canvas_dim to 2000 to trigger adaptive downscaling
        result = merge_geotiffs(
            input_paths=tile_paths,
            output_path=out_path,
            max_canvas_dim=2000,
            auto_rescale=True,
        )

        self.assertTrue(os.path.exists(out_path))
        self.assertEqual(result["inputs_merged"], 12)
        self.assertLessEqual(result["width"], 2000)
        self.assertLessEqual(result["height"], 2000)
        self.assertTrue(result["rescaled"])
        self.assertGreater(result["rescale_factor"], 1.0)

        # Validate saved GeoTIFF tags match rescaled values
        with Image.open(out_path) as out_img:
            saved_scale = out_img.tag_v2.get(33550)
            self.assertIsNotNone(saved_scale)
            self.assertAlmostEqual(saved_scale[0], result["scale_x"], places=8)

    def test_real_boundaries_merge(self):
        """Tests merging 10+ real GeoTIFFs from boundaries/ folder if available."""
        real_files = sorted(glob.glob("boundaries/*.tif"))
        if len(real_files) >= 10:
            selected_10 = real_files[:10]
            out_path = os.path.join(self.test_dir, "merged_real_10.tif")
            result = merge_geotiffs(
                input_paths=selected_10,
                output_path=out_path,
                max_canvas_dim=4000,
                max_megapixels=16.0,
                auto_rescale=True,
            )
            self.assertTrue(os.path.exists(out_path))
            self.assertEqual(result["inputs_merged"], 10)
            self.assertLessEqual(result["width"], 4000)
            self.assertLessEqual(result["height"], 4000)

if __name__ == "__main__":
    unittest.main()
